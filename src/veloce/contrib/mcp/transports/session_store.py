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

Sessions live in this process. Behind more than one worker a client's second
request may reach a worker that never saw its id, which answers 404 and makes the
client start over. A `SessionBackend` closes that: the store consults it for what
a session id means independently of who is serving it, so any worker can pick up
a session another minted.

Only part of a session travels. `initialized`, the client's advertised
capabilities and its `clientInfo` are true wherever the session is served, so they
are what a backend holds. Subscriptions, open listen streams, background tasks and
the in-flight cancellation registry belong to the worker holding the connection -
a task cannot be cancelled from a process that is not running it - so they stay
local and are rebuilt per worker.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

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


@dataclass(slots=True)
class SessionRecord:
    """What a session id means, independently of the worker serving it.

    This is the whole of what a shared backend stores: the lifecycle flag and the
    identity the client declared. Everything else a session owns is bound to one
    worker's connection and is rebuilt there.
    """

    initialized: bool = False
    client_capabilities: dict[str, Any] = field(default_factory=dict)
    client_info: dict[str, Any] | None = None


@runtime_checkable
class SessionBackend(Protocol):
    """Where session records live when more than one worker serves a client.

    The methods are async because a shared backend is I/O - a round trip to Redis
    or a database - and a blocking call would stall the worker's event loop.

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


class _LiveSession:
    """One live `Mcp-Session-Id`: its `MCPSession` and last-touched timestamp."""

    __slots__ = ("session", "touched_at")

    def __init__(self, session: MCPSession) -> None:
        self.session = session
        self.touched_at = time.monotonic()


class HttpSessionStore:
    """Track live `Mcp-Session-Id` values and their sessions for the HTTP transport."""

    __slots__ = ("_live", "_idle_ttl", "_on_evict", "_backend")

    def __init__(
        self,
        idle_ttl: float = _DEFAULT_IDLE_TTL_SECONDS,
        on_evict: Callable[[MCPSession], None] | None = None,
        backend: SessionBackend | None = None,
    ) -> None:
        self._live: dict[str, _LiveSession] = {}
        self._idle_ttl = idle_ttl
        # Where session records are shared with other workers. `None` - the
        # default - keeps sessions in this process, which is what a single-worker
        # deployment wants and costs nothing.
        self._backend = backend
        # Called with a session when its id is terminated or evicted, so the
        # transport can reclaim what the session owns (its subscription connection
        # and its tasks). `None` when no cleanup is needed.
        self._on_evict = on_evict

    async def create(self) -> tuple[str, MCPSession]:
        """Mint a new session id with its `MCPSession`, record it, and return both."""
        self._evict_idle()
        session_id = secrets.token_urlsafe(_SESSION_ID_ENTROPY_BYTES)
        session = MCPSession()
        self._live[session_id] = _LiveSession(session)
        if self._backend is not None:
            await self._backend.write(session_id, _record_of(session), self._idle_ttl)
        return session_id, session

    async def resolve(self, session_id: str) -> MCPSession | None:
        """Return the live session for `session_id`, touching it, or `None`.

        Resolving an id refreshes its idle deadline so an actively-used session is
        never reclaimed; an unknown or already-evicted id returns `None`.

        With a backend the record is read on every resolution, not cached: it is
        how this worker learns that another ended the session or completed its
        handshake. What stays local is the connection-bound state - an id this
        worker has not served before is adopted with that state empty, which is
        what it means for another worker to take over the conversation.
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

        Called after each message so a later request served by another worker sees
        the handshake this one completed. Without a backend there is nobody to
        publish to and this returns immediately.
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
        # The client asked for the session to end, so it ends everywhere - not only
        # on the worker that happened to receive the DELETE. Whether it was live is
        # the backend's answer, since the DELETE may reach a worker that never
        # served it; an id nobody ever minted is still a no-op.
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

        The move is what keeps `_evict_idle` off a full scan: with every touch
        re-inserting, `_live` is ordered oldest-first, so the sweep stops at the
        first entry still inside the window.
        """
        entry.touched_at = time.monotonic()
        del self._live[session_id]
        self._live[session_id] = entry

    def _evict_idle(self) -> None:
        """Reclaim sessions untouched past the idle time-to-live.

        This reclaims what this worker holds. A shared record is left for its own
        expiry: another worker may still be serving the session, and dropping the
        record because *this* worker went quiet would end a live conversation.

        `_live` is ordered oldest-touched first (see `_touch`), so this walks only
        the entries it actually reclaims and stops at the first live one. It runs
        on `resolve`, which is every MCP request, and a full scan there cost one
        pass over every live session per request.
        """
        if not self._live:
            return
        # `<=` so a session exactly at the deadline is reclaimed: with `idle_ttl=0`
        # ("evict on next access") and a coarse monotonic clock (Windows' ~15ms
        # granularity can read the same value twice), a strict `<` would never fire.
        deadline = time.monotonic() - self._idle_ttl
        expired: list[tuple[str, _LiveSession]] = []
        # Iterated lazily and stopped at the first live entry, so the walk is the
        # length of what is being reclaimed - not of the whole map.
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
