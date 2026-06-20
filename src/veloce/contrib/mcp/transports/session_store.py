"""HTTP session store — opt-in `Mcp-Session-Id` lifecycle for the HTTP transport.

The Streamable HTTP transport is stateless by default: each POST is an isolated
JSON-RPC message. When session management is enabled the server assigns an
`Mcp-Session-Id` on the `initialize` result and then ties every later request to
that id — validating the echoed header, answering an unknown or terminated id with
HTTP 404, and accepting a `DELETE` to terminate the session.

This store owns that state behind the `Transport` contract, so the transport's
dispatch path consults one small object instead of growing session bookkeeping
inline. The store is created only when the feature is enabled, so the default
stateless path allocates nothing and pays no per-request cost.
"""

from __future__ import annotations

import secrets

# Bytes of entropy per session id. `secrets.token_urlsafe` yields URL-safe base64
# (characters within the MCP-mandated visible-ASCII range 0x21-0x7E), so the id is
# globally unique and cryptographically secure per the transport spec.
_SESSION_ID_ENTROPY_BYTES = 24


class HttpSessionStore:
    """Track live `Mcp-Session-Id` values for the Streamable HTTP transport."""

    __slots__ = ("_live",)

    def __init__(self) -> None:
        self._live: set[str] = set()

    def create(self) -> str:
        """Mint a new session id, record it as live, and return it."""
        session_id = secrets.token_urlsafe(_SESSION_ID_ENTROPY_BYTES)
        self._live.add(session_id)
        return session_id

    def is_live(self, session_id: str) -> bool:
        """Return whether `session_id` names a session that has not been terminated."""
        return session_id in self._live

    def terminate(self, session_id: str) -> bool:
        """Drop `session_id`; return whether it had been live (a no-op otherwise)."""
        had = session_id in self._live
        self._live.discard(session_id)
        return had
