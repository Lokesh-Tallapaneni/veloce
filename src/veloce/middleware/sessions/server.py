"""Server-side session middleware — a signed cookie carrying only a session id."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from veloce.http.request import Request
from veloce.http.response import Response
from veloce.sessions import InMemorySessionStore, Session, SessionStore

if TYPE_CHECKING:  # pragma: no cover
    pass

from veloce.middleware.sessions._shared import (
    _UNSET,
    _begin_session_response,
    _should_persist,
    _UnsetType,
)
from veloce.middleware.sessions.base import (
    SessionMiddlewareBase,
)


class ServerSessionMiddleware(SessionMiddlewareBase):
    """Server-side session - the cookie carries only an opaque session id.

    The payload lives in a `SessionStore`, not in the cookie, so a session is
    *revocable*: empty it in a handler (`session.clear()`) or delete it from the
    store and it is gone server-side. A tampered or stale cookie simply fails to
    resolve and is treated as a fresh session.

    The default store is a process-local `InMemorySessionStore`; pass a shared
    backend for a multi-worker deployment. The caller owns it - keep a reference
    to revoke sessions by id.

    Set `renew_on_access=True` for sliding expiry: a session that was only read
    has its store TTL refreshed (via `SessionStore.touch`) and its cookie
    re-stamped. Default off.
    """

    def __init__(
        self,
        store: SessionStore | None = None,
        cookie_name: str | _UnsetType = _UNSET,
        max_age: int = 86400 * 14,
        permanent_lifetime: int | _UnsetType = _UNSET,
        path: str | _UnsetType = _UNSET,
        *,
        httponly: bool | _UnsetType = _UNSET,
        secure: bool | _UnsetType = _UNSET,
        samesite: str | None | _UnsetType = _UNSET,
        domain: str | None = None,
        vary_on_cookie: bool = True,
        persist_on_status: Callable[[int], bool] | None = None,
        cookie_prefix: Literal["host", "secure"] | None = None,
        partitioned: bool = False,
        renew_on_access: bool = False,
        name: str | None = None,
    ) -> None:
        """Configure the server-side session.

        Args:
            store: Where session payloads live; a process-local store when omitted.
            cookie_name: Cookie name; the library default when omitted.
            max_age: Seconds a non-permanent session lasts.
            permanent_lifetime: Seconds a `session.permanent` session lasts.
            path: Cookie `Path`; the library default when omitted.
            httponly: Cookie `HttpOnly`; the library default when omitted.
            secure: Cookie `Secure`; the library default when omitted.
            samesite: Cookie `SameSite`; the library default when omitted.
            domain: Cookie `Domain`.
            vary_on_cookie: Add `Vary: Cookie` to responses that read the session.
            persist_on_status: Decides from the status code whether to save the session;
                by default any status below 500.
            cookie_prefix: Apply the `__Host-` or `__Secure-` prefix to the cookie name.
            partitioned: Mark the cookie `Partitioned`.
            renew_on_access: Refresh the lifetime on every request that reads the session.
            name: Name for `exclude_middleware`.

        Raises:
            ValueError: If the cookie settings are an invalid combination, as for
                `SessionMiddleware`.

        """
        super().__init__(name=name)
        self._configure_cookie(
            cookie_name=cookie_name,
            max_age=max_age,
            permanent_lifetime=permanent_lifetime,
            path=path,
            httponly=httponly,
            secure=secure,
            samesite=samesite,
            domain=domain,
            cookie_prefix=cookie_prefix,
            partitioned=partitioned,
            vary_on_cookie=vary_on_cookie,
            persist_on_status=persist_on_status,
            renew_on_access=renew_on_access,
        )
        self.store = store if store is not None else InMemorySessionStore()

    async def process_request(self, request: Request) -> Response | None:
        """Load the session from the server-side store by cookie id."""
        data: dict[str, Any] | None = None
        session_id = request.cookies.get(self._wire_cookie_name)
        if session_id:
            data = await self.store.read(session_id)
        if data is not None:
            session = Session(data)
            session.new = False
            # Stashed so process_response writes back under it.
            request._state["_session_id"] = session_id
        else:
            session = Session()
            session.new = True
        request._state["session"] = session
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Save the modified session back to the server-side store."""
        session = request._state.get("session")
        begun = _begin_session_response(self.vary_on_cookie, session, response)
        if begun is None:
            return response
        accessed, modified = begun

        if not modified:
            # The payload is unchanged, so a TTL slide needs no store write.
            if (
                self.renew_on_access
                and accessed
                and not getattr(session, "new", False)
                and _should_persist(self._persist_on_status, response.status_code)
            ):
                await self._renew(request, response)
            return response

        # Do not persist (store write or cookie change) on a 5xx by default.
        if not _should_persist(self._persist_on_status, response.status_code):
            return response

        session_id = request._state.get("_session_id")
        # One lifetime for the entry and its cookie, so neither outlives the other.
        lifetime = self.cookie_lifetime(session)
        if not session:
            # Emptied by the handler: revoke it and drop the cookie.
            if session_id is not None:
                await self.store.delete(session_id)
                self._delete_cookie(response, self.cookie_name, prefix=True)
            return response

        if session_id is None or session.regenerate:
            # A fresh session, or a rotation whose old id must stop resolving.
            if session_id is not None and session.regenerate:
                await self.store.delete(session_id)
            # Mint an unguessable id - 256 bits of entropy - and create it.
            session_id = secrets.token_urlsafe(32)
            await self.store.write(session_id, dict(session), lifetime)
        else:
            # A concurrent logout may have revoked it, and `write` would
            # resurrect it; `replace` is conditional.
            if not await self.store.replace(session_id, dict(session), lifetime):
                # Revoked under us - honour the revocation and drop the cookie.
                self._delete_cookie(response, self.cookie_name, prefix=True)
                return response
        self._set_session_cookie(response, session_id, lifetime)
        return response

    async def _renew(self, request: Request, response: Response) -> None:
        """Slide the store TTL and cookie `Max-Age` for a read-only access.

        Refreshes the entry's expiry without rewriting its payload. An entry
        revoked under us (`touch` returns False) clears the cookie, as in
        `process_response`.
        """
        session_id = request._state.get("_session_id")
        # The cookie did not resolve on read, so there is nothing to slide.
        if session_id is None:
            return
        # Its own lifetime, or a permanent session is demoted on a mere read.
        lifetime = self.cookie_lifetime(request._state.get("session"))
        if not await self.store.touch(session_id, lifetime):
            self._delete_cookie(response, self.cookie_name, prefix=True)
            return
        self._set_session_cookie(response, session_id, lifetime)

    def _set_session_cookie(self, response: Response, session_id: str, lifetime: int) -> None:
        """Write the opaque session-id cookie with this middleware's attributes.

        One place mapping the cookie attribute set to `set_cookie`, shared by
        write and renew, mirroring the delete-side `_delete_cookie`.
        """
        response.set_cookie(
            self.cookie_name,
            session_id,
            max_age=lifetime,
            path=self.path,
            domain=self.domain,
            httponly=self.httponly,
            secure=self.secure,
            samesite=self.samesite,
            partitioned=self.partitioned,
            prefix=self.cookie_prefix,
        )
