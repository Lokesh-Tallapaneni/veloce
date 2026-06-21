"""HTTP session store — opt-in `Mcp-Session-Id` lifecycle for the HTTP transport.

The Streamable HTTP transport is stateless by default: each POST is an isolated
JSON-RPC message. When session management is enabled the server assigns an
`Mcp-Session-Id` on the `initialize` result and then ties every later request to
that id — validating the echoed header, answering an unknown or terminated id with
HTTP 404, and accepting a `DELETE` to terminate the session.

Each live id owns a real `MCPSession`, so a stateful HTTP connection is a
first-class `MCPServer` connection exactly as the stdio loop's is: it records the
client's advertised capabilities from `initialize`, scopes the in-flight
cancellation registry, holds the connection's resource subscriptions, and bounds
lifecycle ordering. The transport's dispatch path consults one small object behind
the `Transport` contract instead of growing session bookkeeping inline.

A session that a client never `DELETE`s is reclaimed by an idle time-to-live so a
long-running server does not accumulate abandoned ids without bound. The store is
created only when the feature is enabled, so the default stateless path allocates
nothing and pays no per-request cost.
"""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING

from veloce.contrib.mcp.session import MCPSession

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

# Bytes of entropy per session id. `secrets.token_urlsafe` yields URL-safe base64
# (characters within the MCP-mandated visible-ASCII range 0x21-0x7E), so the id is
# globally unique and cryptographically secure per the transport spec.
_SESSION_ID_ENTROPY_BYTES = 24

# Idle seconds a session is retained without being touched before it is reclaimed.
# A client that initializes and disappears (never sending `DELETE`) would otherwise
# leak its id and its `MCPSession` for the process lifetime; eviction runs lazily
# on session resolution so no background timer is needed.
_DEFAULT_IDLE_TTL_SECONDS = 3600.0


class _LiveSession:
    """One live `Mcp-Session-Id`: its `MCPSession` and last-touched timestamp."""

    __slots__ = ("session", "touched_at")

    def __init__(self, session: MCPSession) -> None:
        self.session = session
        self.touched_at = time.monotonic()


class HttpSessionStore:
    """Track live `Mcp-Session-Id` values and their sessions for the HTTP transport."""

    __slots__ = ("_live", "_idle_ttl", "_on_evict")

    def __init__(
        self,
        idle_ttl: float = _DEFAULT_IDLE_TTL_SECONDS,
        on_evict: Callable[[MCPSession], None] | None = None,
    ) -> None:
        self._live: dict[str, _LiveSession] = {}
        self._idle_ttl = idle_ttl
        # Called with a session when its id is terminated or evicted, so the
        # transport can reclaim what the session owns (its subscription connection
        # and its tasks). `None` when no cleanup is needed.
        self._on_evict = on_evict

    def create(self) -> tuple[str, MCPSession]:
        """Mint a new session id with its `MCPSession`, record it, and return both."""
        self._evict_idle()
        session_id = secrets.token_urlsafe(_SESSION_ID_ENTROPY_BYTES)
        session = MCPSession()
        self._live[session_id] = _LiveSession(session)
        return session_id, session

    def resolve(self, session_id: str) -> MCPSession | None:
        """Return the live session for `session_id`, touching it, or `None`.

        Resolving an id refreshes its idle deadline so an actively-used session is
        never reclaimed; an unknown or already-evicted id returns `None`.
        """
        self._evict_idle()
        entry = self._live.get(session_id)
        if entry is None:
            return None
        entry.touched_at = time.monotonic()
        return entry.session

    def terminate(self, session_id: str) -> bool:
        """Drop `session_id`; return whether it had been live (a no-op otherwise)."""
        entry = self._live.pop(session_id, None)
        if entry is None:
            return False
        if self._on_evict is not None:
            self._on_evict(entry.session)
        return True

    def _evict_idle(self) -> None:
        """Reclaim sessions untouched past the idle time-to-live."""
        if not self._live:
            return
        # `<=` so a session exactly at the deadline is reclaimed: with `idle_ttl=0`
        # ("evict on next access") and a coarse monotonic clock (Windows' ~15ms
        # granularity can read the same value twice), a strict `<` would never fire.
        deadline = time.monotonic() - self._idle_ttl
        expired = [sid for sid, entry in self._live.items() if entry.touched_at <= deadline]
        for sid in expired:
            entry = self._live.pop(sid, None)
            if entry is not None and self._on_evict is not None:
                self._on_evict(entry.session)
