"""Cookie-based session middleware - signed + timestamped payload.

The cookie body is produced by `veloce.signing.Signer`, which embeds a
server-side timestamp so `max_age` can be enforced on the server (the
cookie's own `Max-Age` directive is only a client hint - an attacker
can replay an old cookie past its expiry, so we never trust it as the
sole expiry signal).

Secret rotation: pass a list of secrets - the first signs new cookies,
the rest are accepted on read for the rotation window. Old cookies
keep validating until they age out.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from veloce.http.cookies import dump_cookie
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware
from veloce.sessions import InMemorySessionStore, Session, SessionStore
from veloce.signing import BadSignature, Signer

# The logger name is part of the public contract: callers (and the test
# suite) filter on "veloce.sessions", so it stays a literal rather than
# `__name__` (which would resolve to "veloce.middleware.sessions").
_logger = logging.getLogger("veloce.sessions")

# RFC 6265 Sec. 6.1 only mandates 4096 bytes per cookie (name + value + attrs);
# browsers and proxies enforce this inconsistently, so 4093 is the de-facto
# safe ceiling (4096 - 3 bytes of separator overhead some impls reserve).
_DEFAULT_MAX_COOKIE_SIZE = 4093


class SessionMiddleware(Middleware):
    """Server-side session stored in a signed, timestamped cookie."""

    def __init__(
        self,
        secret_key: str | list[str],
        cookie_name: str = "session",
        max_age: int = 86400 * 14,
        path: str = "/",
        httponly: bool = True,
        secure: bool = False,
        samesite: str = "lax",
        permanent_lifetime: int = 86400 * 31,
        max_cookie_size: int = _DEFAULT_MAX_COOKIE_SIZE,
    ) -> None:
        keys = [secret_key] if isinstance(secret_key, str) else list(secret_key)
        if not keys:
            raise ValueError("secret_key must be a non-empty string or list of strings")
        self._signer = Signer(keys[0], salt="veloce.session")
        for fallback in keys[1:]:
            self._signer.add_fallback_secret(fallback, salt="veloce.session")
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.path = path
        self.httponly = httponly
        self.secure = secure
        self.samesite = samesite
        self._samesite_cap = self.samesite.capitalize() if self.samesite else None
        # `PERMANENT_SESSION_LIFETIME` analog - used for the cookie
        # `Max-Age` when `session.permanent` is set. Defaults to 31 days.
        self.permanent_lifetime = permanent_lifetime
        self.max_cookie_size = max_cookie_size

    async def process_request(self, request: Request) -> Response | None:
        """Load the session from the signed cookie into request state."""
        session_data: dict[str, Any] = {}
        is_new = True
        cookie_val = request.cookies.get(self.cookie_name)
        if cookie_val:
            try:
                # Read with the longer window so a permanent cookie is
                # not rejected; a non-permanent cookie's shorter
                # `Max-Age` already evicts it client-side.
                decoded = self._signer.loads(
                    cookie_val, max_age=max(self.max_age, self.permanent_lifetime)
                )
            except BadSignature:
                decoded = None
            if isinstance(decoded, dict):
                session_data = decoded
                is_new = False
        session = Session(session_data)
        # `new` is True when the request carried no valid session cookie.
        session.new = is_new
        request._state["session"] = session
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Save the modified session back into the signed cookie."""
        session = request._state.get("session")
        # `Session` flips `.modified` on any mutating operation, so we can
        # skip the re-sign + Set-Cookie when the handler never touched it.
        # No session attached (handler bypassed middleware?) -> nothing to do.
        if session is None or not getattr(session, "modified", False):
            return response

        if not session:
            response.delete_cookie(
                self.cookie_name,
                path=self.path,
                secure=self.secure,
                httponly=self.httponly,
                samesite=self.samesite,
            )
            return response

        cookie_value = self._signer.dumps(session)
        # A `permanent` session uses the longer lifetime for `Max-Age`.
        lifetime = self.permanent_lifetime if getattr(session, "permanent", False) else self.max_age
        # Browsers silently truncate Set-Cookie above ~4 KB, which corrupts
        # the session on the next request. Measure the rendered header and
        # drop the cookie (with a warning) rather than raising - a raise here
        # re-enters this middleware via the error-response path and would
        # propagate as an unhandled ASGI exception. RFC 6265 Sec. 6.1.
        rendered = dump_cookie(
            self.cookie_name,
            cookie_value,
            max_age=lifetime,
            path=self.path,
            httponly=self.httponly,
            secure=self.secure,
            samesite=self._samesite_cap,
        )
        # Measure the on-the-wire byte length, not the character count: a
        # non-ASCII cookie_name/path/domain would otherwise under-count and
        # let the Set-Cookie line exceed the browser's ~4 KB truncation limit
        # without tripping this guard. Cookie headers serialise as latin-1.
        rendered_size = len(rendered.encode("latin-1"))
        if rendered_size > self.max_cookie_size:
            _logger.warning(
                "Session cookie %r is %d bytes, exceeds max_cookie_size=%d; "
                "dropping Set-Cookie on this response. Switch to "
                "ServerSessionMiddleware for payloads of this size.",
                self.cookie_name,
                rendered_size,
                self.max_cookie_size,
            )
            return response
        # `rendered` already holds the fully serialised Set-Cookie line built
        # from this middleware's own (validated) `__init__` parameters, so it
        # is appended directly rather than re-serialised through set_cookie.
        response._append_set_cookie_header(rendered)
        response._encoded = None
        return response


class ServerSessionMiddleware(Middleware):
    """Server-side session - the cookie carries only an opaque session id.

    The session payload lives in a `SessionStore`, not in the cookie, so a
    session is *revocable*: empty it in a handler (`session.clear()`) or
    delete it straight from the store (`await store.delete(session_id)`)
    and it is gone server-side. A tampered or stale cookie simply fails to
    resolve to a stored payload and is treated as a fresh session.

    The default store is a process-local `InMemorySessionStore`; pass a
    shared backend (e.g. a Redis-backed `SessionStore`) for a multi-worker
    deployment. The store is a plain object the caller owns - keep a
    reference to it to revoke sessions by id.
    """

    def __init__(
        self,
        store: SessionStore | None = None,
        cookie_name: str = "session",
        max_age: int = 86400 * 14,
        path: str = "/",
        httponly: bool = True,
        secure: bool = False,
        samesite: str = "lax",
    ) -> None:
        self.store = store if store is not None else InMemorySessionStore()
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.path = path
        self.httponly = httponly
        self.secure = secure
        self.samesite = samesite

    async def process_request(self, request: Request) -> Response | None:
        """Load the session from the server-side store by cookie id."""
        data: dict[str, Any] | None = None
        session_id = request.cookies.get(self.cookie_name)
        if session_id:
            data = await self.store.read(session_id)
        if data is not None:
            session = Session(data)
            session.new = False
            # Stash the id so process_response writes back under it.
            request._state["_session_id"] = session_id
        else:
            session = Session()
            session.new = True
        request._state["session"] = session
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Save the modified session back to the server-side store."""
        session = request._state.get("session")
        if session is None or not getattr(session, "modified", False):
            return response

        session_id = request._state.get("_session_id")
        if not session:
            # The handler emptied the session - revoke it server-side and
            # tell the client to drop the cookie.
            if session_id is not None:
                await self.store.delete(session_id)
                self._clear_session_cookie(response)
            return response

        if session_id is None or session.regenerate:
            # A fresh session, or an explicit id rotation requested via
            # `session.regenerate_id()` at a privilege boundary. Drop the
            # old store entry so the previous id can no longer resolve.
            if session_id is not None and session.regenerate:
                await self.store.delete(session_id)
            # Mint an unguessable id - 256 bits of entropy - and create it.
            session_id = secrets.token_urlsafe(32)
            await self.store.write(session_id, dict(session), self.max_age)
        else:
            # An already-stored session: write back only if it still
            # exists. A concurrent request may have revoked it (logout,
            # `store.delete(...)`) while this one was in flight - a plain
            # `write` would resurrect it, so use the conditional `replace`.
            if not await self.store.replace(session_id, dict(session), self.max_age):
                # Revoked under us - honour the revocation and drop the cookie.
                self._clear_session_cookie(response)
                return response
        response.set_cookie(
            self.cookie_name,
            session_id,
            max_age=self.max_age,
            path=self.path,
            httponly=self.httponly,
            secure=self.secure,
            samesite=self.samesite,
        )
        return response

    def _clear_session_cookie(self, response: Response) -> None:
        """Tell the client to drop the session cookie. Single place that
        knows how this middleware's cookie attribute set maps to
        `delete_cookie` - three callers all share the same kwargs."""
        response.delete_cookie(
            self.cookie_name,
            path=self.path,
            secure=self.secure,
            httponly=self.httponly,
            samesite=self.samesite,
        )
