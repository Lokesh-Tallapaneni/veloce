"""Cookie-based session middleware — signed + timestamped payload.

The cookie body is produced by `veloce.signing.Signer`, which embeds a
server-side timestamp so `max_age` is enforced on the server: the cookie's own
`Max-Age` is a client hint an attacker ignores when replaying a stolen cookie,
so it is never the sole expiry signal.

Secret rotation: pass a list of secrets - the first signs new cookies, the rest
are accepted on read until the old cookies age out.
"""

from __future__ import annotations

from veloce.middleware.sessions._shared import (
    _CHUNK_SEP,
    _DEFAULT_MAX_COOKIE_CHUNKS,
    _DEFAULT_MAX_COOKIE_SIZE,
    _RETIRED_CONFIG_KEYS,
    _SHARED_COOKIE_DEFAULTS,
    _UNSET,
    _begin_session_response,
    _build_signer,
    _reassemble_chunks,
    _shared_cookie_settings,
    _should_persist,
    _UnsetType,
    _validate_cookie_security,
    _wire_name,
)
from veloce.middleware.sessions.base import SessionMiddlewareBase
from veloce.middleware.sessions.cookie import SessionMiddleware
from veloce.middleware.sessions.server import ServerSessionMiddleware

__all__ = [
    "ServerSessionMiddleware",
    "SessionMiddleware",
    "SessionMiddlewareBase",
]
