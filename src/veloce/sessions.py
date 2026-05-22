"""Session container — a dict that tracks mutation and newness.

`SessionMiddleware` stores one of these on every request. `new` reports
whether the request arrived without a valid session cookie; `modified`
flips to `True` the first time any mutating operation runs, so callers
can cheaply tell whether the session needs to be written back.
`permanent` selects the longer `permanent_lifetime` for
the session cookie's `Max-Age` instead of the default `max_age`.
"""

from __future__ import annotations

import time
from typing import Any


class Session(dict[str, Any]):
    """The request session — a dict that knows when it has changed."""

    __slots__ = ("new", "modified")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # dict's C-level init populates without routing through our
        # `__setitem__`, so the initial load never marks the session
        # modified.
        super().__init__(*args, **kwargs)
        self.new = False
        self.modified = False

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

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(key, value)
        self.modified = True

    def __delitem__(self, key: Any) -> None:
        super().__delitem__(key)
        self.modified = True

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
        self.modified = True


class SessionStore:
    """Server-side session backend interface.

    A concrete store persists session payloads keyed by an opaque session
    id; `ServerSessionMiddleware` drives it. The methods are async so a
    network-backed store (Redis, a database) can implement them without
    blocking the event loop — the bundled `InMemorySessionStore` satisfies
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
        """Revoke `session_id` — a later `read` of it must return `None`."""
        raise NotImplementedError


class InMemorySessionStore(SessionStore):
    """A process-local `SessionStore` — a dict with per-entry expiry.

    Fine for a single-process app and for tests. It does not share state
    across workers, so a multi-worker deployment needs a shared backend
    (e.g. Redis) implementing the `SessionStore` interface.
    """

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        # session_id -> (payload, unix-timestamp when it expires)
        self._entries: dict[str, tuple[dict[str, Any], float]] = {}

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

    async def delete(self, session_id: str) -> None:
        self._entries.pop(session_id, None)
