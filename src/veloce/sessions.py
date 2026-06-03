"""Session container - a dict that tracks mutation and newness.

`SessionMiddleware` stores one of these on every request. `new` reports
whether the request arrived without a valid session cookie; `modified`
flips to `True` the first time any mutating operation runs, so callers
can cheaply tell whether the session needs to be written back.
`permanent` selects the longer `permanent_lifetime` for
the session cookie's `Max-Age` instead of the default `max_age`.
"""

from __future__ import annotations

import random
import time
from typing import Any

# Probabilistic sweep tuning for `InMemorySessionStore`. The threshold keeps
# small stores cheap; the probability keeps the amortised cost of a write
# below one full scan per `1/_SWEEP_PROBABILITY` writes. Mirrors Django's
# `cached_db` session backend.
_SWEEP_THRESHOLD = 1000
_SWEEP_PROBABILITY = 1.0 / 32


class Session(dict[str, Any]):
    """The request session - a dict that knows when it has changed."""

    __slots__ = ("new", "modified", "regenerate", "accessed")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # dict's C-level init populates without routing through our
        # `__setitem__`, so the initial load never marks the session
        # modified.
        super().__init__(*args, **kwargs)
        self.new = False
        self.modified = False
        # Set by `regenerate_id()` - asks a server-side session backend to
        # mint a fresh id for this session on the next response.
        self.regenerate = False
        # Flipped True by `Request.session` when a handler reads or writes the
        # session, so the middleware emits `Vary: Cookie` only on responses
        # that actually depend on session contents - not merely because an
        # (even stale) session cookie was present. The initial load does not
        # set it; only handler-side access does.
        self.accessed = False

    @property
    def permanent(self) -> bool:
        """Whether the session cookie should use the longer lifetime.

        backed by the reserved `_permanent` key, so the
        flag persists in the cookie across requests and toggling it
        counts as a session mutation.
        """
        return bool(self.get("_permanent", False))

    @permanent.setter
    def permanent(self, value: bool) -> None:
        self["_permanent"] = bool(value)

    def regenerate_id(self) -> None:
        """Request a fresh server-side session id on the next response.

        Call this at a privilege boundary - login, role change - so a
        pre-existing (possibly attacker-planted) session id cannot be
        replayed against the now-elevated session: the session-fixation
        defence. It marks the session modified so the rotation is written
        back. Harmless with cookie-only sessions, which carry no
        server-side id to rotate.
        """
        self.regenerate = True
        self.modified = True

    # -- Mutation tracking --------------------------------------
    # Every mutating dict operation is overridden to flip `modified`,
    # so the cookie middleware can cheaply tell when a re-write is due.

    def clear(self) -> None:
        super().clear()
        self.modified = True

    def pop(self, key: Any, *default: Any) -> Any:
        had = key in self
        result = super().pop(key, *default)
        if had:
            self.modified = True
        return result

    def popitem(self) -> Any:
        result = super().popitem()
        self.modified = True
        return result

    def setdefault(self, key: Any, default: Any = None) -> Any:
        existed = key in self
        result = super().setdefault(key, default)
        if not existed:
            self.modified = True
        return result

    def update(self, *args: Any, **kwargs: Any) -> None:
        super().update(*args, **kwargs)
        # Skip the cookie re-write only for the unambiguously-empty case
        # (`session.update({})` / `session.update()` with no kwargs).
        # Non-empty mappings flip `modified`; non-mapping arguments
        # (iterators of key-value pairs, generators) cannot be checked
        # for emptiness after the super().update consumed them, so they
        # conservatively flip `modified` too.
        if kwargs:
            self.modified = True
            return
        if not args:
            return
        first = args[0]
        if isinstance(first, dict) and not first:
            return
        self.modified = True

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(key, value)
        self.modified = True

    def __delitem__(self, key: Any) -> None:
        super().__delitem__(key)
        self.modified = True

    def __ior__(self, other: Any) -> Session:  # type: ignore[override,misc]
        # PEP 584 `|=` goes through the C-level merge, not `__setitem__`.
        # Without this override the mutation is invisible to `.modified`
        # and the cookie middleware would skip the re-sign. The `misc`
        # ignore is the standard workaround for dict's `__or__` / `__ior__`
        # signature interaction with subclasses (see python/mypy#3553).
        super().__ior__(other)
        self.modified = True
        return self


class SessionStore:
    """Server-side session backend interface.

    A concrete store persists session payloads keyed by an opaque session
    id; `ServerSessionMiddleware` drives it. The methods are async so a
    network-backed store (Redis, a database) can implement them without
    blocking the event loop - the bundled `InMemorySessionStore` satisfies
    the contract without any real awaiting.
    """

    async def read(self, session_id: str) -> dict[str, Any] | None:
        """Return the stored payload for `session_id`, or `None` when it
        is absent, expired, or has been revoked."""
        raise NotImplementedError

    async def write(self, session_id: str, data: dict[str, Any], max_age: int) -> None:
        """Persist `data` under `session_id`, to expire after `max_age` seconds."""
        raise NotImplementedError

    async def delete(self, session_id: str) -> None:
        """Revoke `session_id` - a later `read` of it must return `None`."""
        raise NotImplementedError

    async def replace(self, session_id: str, data: dict[str, Any], max_age: int) -> bool:
        """Write `data` for `session_id` **only if it still exists**.

        Returns `True` on success, `False` when the id is absent - it was
        revoked or expired. This is the race-safe write the middleware
        uses for an already-stored session, so a request still in flight
        cannot resurrect a session a concurrent `delete` removed.

        The default is a non-atomic read-then-write; a store with an
        atomic conditional write (Redis `SET ... XX`, a DB `UPDATE`)
        should override this to close the check-then-write window.
        """
        if await self.read(session_id) is None:
            return False
        await self.write(session_id, data, max_age)
        return True


class InMemorySessionStore(SessionStore):
    """A process-local `SessionStore` - a dict with per-entry expiry.

    Fine for a single-process app and for tests. It does not share state
    across workers, so a multi-worker deployment needs a shared backend
    (e.g. Redis) implementing the `SessionStore` interface.
    """

    __slots__ = ("_entries", "_sweep_threshold", "_sweep_probability")

    def __init__(
        self,
        sweep_threshold: int = _SWEEP_THRESHOLD,
        sweep_probability: float = _SWEEP_PROBABILITY,
    ) -> None:
        # session_id -> (payload, unix-timestamp when it expires)
        self._entries: dict[str, tuple[dict[str, Any], float]] = {}
        # Per-instance sweep tuning; defaults to the module constants so
        # existing call sites are unaffected.
        self._sweep_threshold = sweep_threshold
        self._sweep_probability = sweep_probability

    async def read(self, session_id: str) -> dict[str, Any] | None:
        entry = self._entries.get(session_id)
        if entry is None:
            return None
        data, expires_at = entry
        if expires_at <= time.time():
            del self._entries[session_id]  # lazily evict on access
            return None
        return dict(data)

    async def write(self, session_id: str, data: dict[str, Any], max_age: int) -> None:
        self._entries[session_id] = (dict(data), time.time() + max_age)
        self._maybe_sweep()

    async def delete(self, session_id: str) -> None:
        self._entries.pop(session_id, None)

    async def replace(self, session_id: str, data: dict[str, Any], max_age: int) -> bool:
        # Atomic against the event loop: this coroutine does no `await`,
        # so a concurrent `delete` cannot land between the check and the
        # write - a revoked session stays revoked. An expired entry that
        # has not yet been lazily evicted counts as absent (mirrors
        # `read`), so a stale session is never rewritten back to life.
        entry = self._entries.get(session_id)
        if entry is None or entry[1] <= time.time():
            self._entries.pop(session_id, None)
            return False
        self._entries[session_id] = (dict(data), time.time() + max_age)
        self._maybe_sweep()
        return True

    def sweep_expired(self) -> int:
        """Drop every expired entry and return how many were removed.

        Callers that want deterministic eviction (e.g. a background task
        on a known cadence) can call this directly rather than relying on
        the probabilistic sweep that fires from `write` / `replace`.
        """
        now = time.time()
        # Snapshot first - a concurrent write during iteration would otherwise
        # raise `RuntimeError: dictionary changed size during iteration`. Use
        # `pop(..., None)` so a concurrent delete of the same id is a no-op.
        expired = [sid for sid, (_, exp) in list(self._entries.items()) if exp <= now]
        removed = 0
        for sid in expired:
            if self._entries.pop(sid, None) is not None:
                removed += 1
        return removed

    def _maybe_sweep(self) -> None:
        # Amortised eviction: only walk the store when it's grown past
        # the threshold and the dice come up. Keeps the per-write cost
        # at one comparison + one `random.random()` in the common case.
        if (
            len(self._entries) >= self._sweep_threshold
            and random.random() < self._sweep_probability
        ):
            self.sweep_expired()
