"""Session container — a dict that tracks mutation and newness.

`new` reports that the request arrived without a valid session cookie;
`modified` flips on the first mutating operation, so a caller can cheaply tell
whether the session needs writing back; `permanent` selects the longer
`permanent_lifetime` for the cookie's `Max-Age`.

The session cookie's `Max-Age`/`Expires` semantics follow RFC 6265 Sec. 5.2.
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Any

from veloce._internal import _require_methods

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

# Sweep tuning: under one full scan per `1/_SWEEP_PROBABILITY` writes.
_SWEEP_THRESHOLD = 1000
_SWEEP_PROBABILITY = 1.0 / 32


class Session(dict[str, Any]):
    """The request session - a dict that knows when it has changed."""

    __slots__ = ("new", "modified", "regenerate", "accessed")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # dict's C-level init bypasses `__setitem__`, so a load is not a change.
        super().__init__(*args, **kwargs)
        self.new = False
        self.modified = False
        # Set by `regenerate_id()` - mint a fresh backend id on the next response.
        self.regenerate = False
        # Flipped by `Request.session`, so `Vary: Cookie` tracks session use
        # rather than the mere presence of a (possibly stale) cookie.
        self.accessed = False

    @property
    def permanent(self) -> bool:
        """Whether the session cookie should use the longer lifetime.

        Backed by the reserved `_permanent` key, so the flag persists in the
        cookie across requests and toggling it counts as a session mutation.
        """
        return bool(self.get("_permanent", False))

    @permanent.setter
    def permanent(self, value: bool) -> None:
        self["_permanent"] = bool(value)

    def regenerate_id(self) -> None:
        """Request a fresh server-side session id on the next response.

        Call this at a privilege boundary - login, role change - so an
        attacker-planted id cannot be replayed against the now-elevated session:
        the session-fixation defence. Harmless with cookie-only sessions.
        """
        self.regenerate = True
        self.modified = True

    # ── Mutation tracking ─────────────────────────────────
    # Every mutating dict operation is overridden to flip `modified`.

    def clear(self) -> None:
        """Remove every key and mark the session modified."""
        super().clear()
        self.modified = True

    def pop(self, key: Any, *default: Any) -> Any:
        """Remove `key` and return its value, marking the session modified on removal."""
        had = key in self
        result = super().pop(key, *default)
        if had:
            self.modified = True
        return result

    def popitem(self) -> Any:
        """Remove and return the last `(key, value)` pair, marking the session modified."""
        result = super().popitem()
        self.modified = True
        return result

    def setdefault(self, key: Any, default: Any = None) -> Any:
        """Insert `default` under `key` when absent, marking the session modified on insert."""
        existed = key in self
        result = super().setdefault(key, default)
        if not existed:
            self.modified = True
        return result

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Merge keys into the session, marking it modified unless the input is empty."""
        super().update(*args, **kwargs)
        # A consumed iterator cannot be tested for emptiness, so it counts as a
        # change; only an unambiguously empty mapping skips the re-write.
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
        # PEP 584 `|=` bypasses `__setitem__`, so `.modified` would miss it.
        super().__ior__(other)
        self.modified = True
        return self


class SessionStore:
    """Server-side session backend interface.

    A concrete store persists session payloads keyed by an opaque session id;
    `ServerSessionMiddleware` drives it. The methods are async so a
    network-backed store can implement them without blocking the event loop.
    """

    # Empty so a slotted concrete store actually stays slotted.
    __slots__ = ()

    #: Methods a store must supply, checked at definition, not on a live request.
    _required = ("read", "write", "delete")

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _require_methods(cls, SessionStore, SessionStore._required)

    async def read(self, session_id: str) -> dict[str, Any] | None:
        """Return the stored payload for `session_id`, or `None`.

        `None` when the session is absent, expired, or has been revoked.
        """
        raise NotImplementedError

    async def write(self, session_id: str, data: dict[str, Any], max_age: int) -> None:
        """Persist `data` under `session_id`, to expire after `max_age` seconds."""
        raise NotImplementedError

    async def delete(self, session_id: str) -> None:
        """Revoke `session_id` - a later `read` of it must return `None`."""
        raise NotImplementedError

    async def replace(self, session_id: str, data: dict[str, Any], max_age: int) -> bool:
        """Write `data` for `session_id` **only if it still exists**.

        `False` when the id is absent - revoked or expired. This is the race-safe
        write, so a request in flight cannot resurrect a session a concurrent
        `delete` removed. The default is a non-atomic read-then-write; a store
        with an atomic conditional write should override it.
        """
        if await self.read(session_id) is None:
            return False
        await self.write(session_id, data, max_age)
        return True

    async def touch(self, session_id: str, max_age: int) -> bool:
        """Extend the expiry of an existing entry without rewriting its payload.

        `False` when the id was absent (revoked or expired). The default reads
        then rewrites the payload; a store with a native TTL-refresh primitive
        should override it.
        """
        data = await self.read(session_id)
        if data is None:
            return False
        await self.write(session_id, data, max_age)
        return True


class InMemorySessionStore(SessionStore):
    """A process-local `SessionStore` - a dict with per-entry expiry.

    It does not share state across workers, so a multi-worker deployment needs a
    shared backend implementing the `SessionStore` interface.

    Sized, iterable and containment-testable, so it is falsy when it holds no
    live session. Test `store is not None` to ask whether one is configured.
    """

    __slots__ = ("_entries", "_sweep_threshold", "_sweep_probability")

    def __init__(
        self,
        sweep_threshold: int = _SWEEP_THRESHOLD,
        sweep_probability: float = _SWEEP_PROBABILITY,
    ) -> None:
        # session_id -> (payload, unix-timestamp when it expires)
        self._entries: dict[str, tuple[dict[str, Any], float]] = {}
        self._sweep_threshold = sweep_threshold
        self._sweep_probability = sweep_probability

    async def read(self, session_id: str) -> dict[str, Any] | None:
        """Return a copy of the stored payload, or `None` when absent or expired."""
        entry = self._entries.get(session_id)
        if entry is None:
            return None
        data, expires_at = entry
        if expires_at <= time.time():
            del self._entries[session_id]  # lazily evict on access
            return None
        return dict(data)

    async def write(self, session_id: str, data: dict[str, Any], max_age: int) -> None:
        """Store a copy of `data` under `session_id`, expiring after `max_age` seconds."""
        self._entries[session_id] = (dict(data), time.time() + max_age)
        self._maybe_sweep()

    async def delete(self, session_id: str) -> None:
        """Drop `session_id` from the store. No-op if not present."""
        self._entries.pop(session_id, None)

    async def replace(self, session_id: str, data: dict[str, Any], max_age: int) -> bool:
        """Write `data` only when `session_id` still exists and is unexpired."""
        # Atomic against the loop: no `await`, so a concurrent `delete` cannot
        # land between check and write. A not-yet-evicted entry counts as absent.
        now = time.time()
        entry = self._entries.get(session_id)
        if entry is None or entry[1] <= now:
            self._entries.pop(session_id, None)
            return False
        self._entries[session_id] = (dict(data), now + max_age)
        self._maybe_sweep()
        return True

    async def touch(self, session_id: str, max_age: int) -> bool:
        """Refresh the expiry of an existing, unexpired entry without copying its payload."""
        # Same liveness check as `replace`: a stale session is never revived.
        now = time.time()
        entry = self._entries.get(session_id)
        if entry is None or entry[1] <= now:
            self._entries.pop(session_id, None)
            return False
        self._entries[session_id] = (entry[0], now + max_age)
        return True

    def sweep_expired(self) -> int:
        """Drop every expired entry and return how many were removed.

        For deterministic eviction, instead of the probabilistic sweep that
        fires from `write` / `replace`.
        """
        now = time.time()
        # The comprehension is its own snapshot; `pop(..., None)` tolerates a race.
        expired = [sid for sid, (_, exp) in self._entries.items() if exp <= now]
        removed = 0
        for sid in expired:
            if self._entries.pop(sid, None) is not None:
                removed += 1
        return removed

    def clear(self) -> int:
        """Drop every session and return how many were removed.

        The sync counterpart to `delete` for the whole store. Expired entries no
        sweep has reached count as removed too.
        """
        removed = len(self._entries)
        self._entries.clear()
        return removed

    # ── Reading the store without going through a session ──

    def expires_at(self, session_id: str) -> float | None:
        """Return when `session_id` expires as a Unix timestamp, or `None`.

        `None` means absent or already past its expiry, matching what `read`
        would say. This is what sliding expiry is observable through.
        """
        entry = self._entries.get(session_id)
        if entry is None or entry[1] <= time.time():
            return None
        return entry[1]

    def __contains__(self, session_id: object) -> bool:
        """Whether `session_id` is stored and unexpired."""
        return isinstance(session_id, str) and self.expires_at(session_id) is not None

    def __len__(self) -> int:
        """How many unexpired sessions the store holds.

        Expired entries are excluded whether or not a sweep has reached them.
        """
        now = time.time()
        return sum(1 for _, expires_at in self._entries.values() if expires_at > now)

    def __iter__(self) -> Iterator[str]:
        """Iterate the unexpired session ids, in insertion order."""
        now = time.time()
        return iter([sid for sid, (_, exp) in self._entries.items() if exp > now])

    def _maybe_sweep(self) -> None:
        # Amortised: one comparison plus one `random.random()` per write.
        if (
            len(self._entries) >= self._sweep_threshold
            and random.random() < self._sweep_probability
        ):
            self.sweep_expired()
