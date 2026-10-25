"""HTTP session store — opt-in `Mcp-Session-Id` lifecycle for the HTTP transport.

The Streamable HTTP transport is stateless by default. With session management
enabled the server assigns an `Mcp-Session-Id` on the `initialize` result and
ties every later request to it: the echoed header is validated, an unknown or
terminated id answers HTTP 404, and a `DELETE` terminates the session.

Each live id owns a real `MCPSession`, so a stateful HTTP connection is a
first-class `MCPServer` connection exactly as the stdio loop's is. A session the
client never `DELETE`s is reclaimed by an idle time-to-live. The store exists
only when the feature is enabled, so the default path pays no per-request cost.

Sessions live in this process, so behind more than one worker a client's second
request may reach a worker that never saw its id. A `SessionBackend` closes that:
the store consults it for what an id means independently of who serves it.

Only part of a session travels. `initialized`, the advertised capabilities and
`clientInfo` are true wherever the session is served, so they are what a backend
holds. Subscriptions, listen streams, tasks and the cancellation registry belong
to the worker holding the connection - a task cannot be cancelled from a process
that is not running it - so they stay local and are rebuilt per worker.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.transports._common import _SESSION_ID_ENTROPY_BYTES

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

# Per worker: generous for a real deployment, but a ceiling clients cannot lift.
_DEFAULT_MAX_SESSIONS = 10_000

# Idle seconds before a session is reclaimed, lazily on resolution not on a timer.
_DEFAULT_IDLE_TTL_SECONDS = 3600.0


# ── Records and backends ──────────────────────────────────


@dataclass(slots=True)
class SessionRecord:
    """What a session id means, independently of the worker serving it.

    The whole of what a shared backend stores; everything else a session owns is
    bound to one worker's connection and is rebuilt there.
    """

    initialized: bool = False
    client_capabilities: dict[str, Any] = field(default_factory=dict)
    client_info: dict[str, Any] | None = None


@runtime_checkable
class SessionBackend(Protocol):
    """Where session records live when more than one worker serves a client.

    The methods are async because a shared backend is I/O, and a blocking call
    would stall the worker's event loop.

    Usage::

        class RedisSessions:
            def __init__(self, client):
                self._client = client

            async def read(self, session_id):
                raw = await self._client.get(f"mcp:{session_id}")
                return None if raw is None else SessionRecord(**json.loads(raw))

            async def write(self, session_id, record, ttl):
                await self._client.set(
                    f"mcp:{session_id}", json.dumps(asdict(record)), ex=int(ttl)
                )

            async def delete(self, session_id):
                await self._client.delete(f"mcp:{session_id}")

        app.mount_mcp(transport="http", sessions=True, session_backend=RedisSessions(redis))
    """

    async def read(self, session_id: str) -> SessionRecord | None:
        """Return the record for `session_id`, or `None` if it is not live."""
        ...

    async def write(self, session_id: str, record: SessionRecord, ttl: float) -> None:
        """Store `record` under `session_id`, expiring it after `ttl` idle seconds."""
        ...

    async def delete(self, session_id: str) -> None:
        """Drop `session_id`, whether or not it was live."""
        ...


# ── The store ─────────────────────────────────────────────


class _LiveSession:
    """One live `Mcp-Session-Id`: its `MCPSession` and last-touched timestamp."""

    __slots__ = ("session", "touched_at")

    def __init__(self, session: MCPSession) -> None:
        self.session = session
        self.touched_at = time.monotonic()


class HttpSessionStore:
    """Track live `Mcp-Session-Id` values and their sessions for the HTTP transport."""

    __slots__ = ("_live", "_idle_ttl", "_max_sessions", "_on_evict", "_backend")

    def __init__(
        self,
        idle_ttl: float = _DEFAULT_IDLE_TTL_SECONDS,
        on_evict: Callable[[MCPSession], None] | None = None,
        backend: SessionBackend | None = None,
        max_sessions: int = _DEFAULT_MAX_SESSIONS,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be >= 1")
        self._live: dict[str, _LiveSession] = {}
        self._idle_ttl = idle_ttl
        # Bounded independently of the TTL: minting a session costs one request.
        self._max_sessions = max_sessions
        # `None` keeps sessions in this process, which is what one worker wants.
        self._backend = backend
        self._on_evict = on_evict

    async def create(self) -> tuple[str, MCPSession]:
        """Mint a new session id with its `MCPSession`, record it, and return both."""
        self._evict_idle()
        # Reclaimed rather than refused, so a flood cannot lock clients out.
        while len(self._live) >= self._max_sessions:
            oldest_id, oldest = next(iter(self._live.items()))
            del self._live[oldest_id]
            if self._on_evict is not None:
                self._on_evict(oldest.session)
        session_id = secrets.token_urlsafe(_SESSION_ID_ENTROPY_BYTES)
        session = MCPSession()
        self._live[session_id] = _LiveSession(session)
        if self._backend is not None:
            await self._backend.write(session_id, _record_of(session), self._idle_ttl)
        return session_id, session

    async def resolve(self, session_id: str) -> MCPSession | None:
        """Return the live session for `session_id`, touching it, or `None`.

        Resolving refreshes the idle deadline, so an actively-used session is
        never reclaimed. With a backend the record is read every time rather than
        cached, which is how this worker learns that another ended the session or
        completed its handshake; connection-bound state stays local, so an id
        this worker has not served is adopted with that state empty.
        """
        self._evict_idle()
        if self._backend is None:
            entry = self._live.get(session_id)
            if entry is None:
                return None
            self._touch(session_id, entry)
            return entry.session

        record = await self._backend.read(session_id)
        if record is None:
            # Ended or expired elsewhere: this worker's copy is stale.
            self._drop_local(session_id)
            return None
        entry = self._live.get(session_id)
        if entry is None:
            entry = _LiveSession(MCPSession())
            self._live[session_id] = entry
        else:
            self._touch(session_id, entry)
        session = entry.session
        session.initialized = record.initialized
        session.client_capabilities = record.client_capabilities
        session.client_info = record.client_info
        return session

    async def persist(self, session_id: str, session: MCPSession) -> None:
        """Publish what a dispatch changed about `session` to the backend.

        Called after each message, so a later request on another worker sees the
        handshake this one completed. Without a backend it returns immediately.
        """
        if self._backend is None:
            return
        await self._backend.write(session_id, _record_of(session), self._idle_ttl)

    async def terminate(self, session_id: str) -> bool:
        """Drop `session_id`; return whether it had been live (a no-op otherwise)."""
        entry = self._live.pop(session_id, None)
        if entry is not None and self._on_evict is not None:
            self._on_evict(entry.session)
        if self._backend is None:
            return entry is not None
        # Ends everywhere, not only on the worker that received the DELETE.
        was_live = await self._backend.read(session_id) is not None
        await self._backend.delete(session_id)
        return was_live or entry is not None

    def _drop_local(self, session_id: str) -> None:
        """Release what this worker holds for a session that is no longer live."""
        entry = self._live.pop(session_id, None)
        if entry is not None and self._on_evict is not None:
            self._on_evict(entry.session)

    def _touch(self, session_id: str, entry: _LiveSession) -> None:
        """Refresh an entry's deadline and move it to the end of `_live`.

        The move keeps `_live` ordered oldest-first, so `_evict_idle` stops at the
        first entry still inside the window instead of scanning.
        """
        entry.touched_at = time.monotonic()
        del self._live[session_id]
        self._live[session_id] = entry

    def _evict_idle(self) -> None:
        """Reclaim sessions untouched past the idle time-to-live.

        Only what this worker holds: a shared record is left for its own expiry,
        since another worker may still be serving the session. Runs on `resolve`,
        so it walks only the entries it reclaims and stops at the first live one.
        """
        if not self._live:
            return
        # With `idle_ttl=0` and Windows' ~15 ms clock, a strict `<` never fires.
        deadline = time.monotonic() - self._idle_ttl
        expired: list[tuple[str, _LiveSession]] = []
        for sid, entry in self._live.items():
            if entry.touched_at > deadline:
                break
            expired.append((sid, entry))
        for sid, entry in expired:
            del self._live[sid]
            if self._on_evict is not None:
                self._on_evict(entry.session)


def _record_of(session: MCPSession) -> SessionRecord:
    """Return the portable part of `session`."""
    return SessionRecord(
        initialized=session.initialized,
        client_capabilities=session.client_capabilities,
        client_info=session.client_info,
    )
