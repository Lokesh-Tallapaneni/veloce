"""Session middleware base — the cookie handling both session middlewares share."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from veloce.audit import Finding
from veloce.http.response import Response
from veloce.middleware.base import Middleware

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    from veloce.audit import AuditContext

from veloce.enums import CookiePrefix
from veloce.middleware.sessions._shared import (
    _RETIRED_CONFIG_KEYS,
    _shared_cookie_settings,
    _UnsetType,
    _validate_cookie_security,
    _wire_name,
)


class SessionMiddlewareBase(Middleware):
    """Base class for a session middleware — subclass it to add a backend.

    Subclassing inherits the documented `session.permanent` rule (a permanent
    session takes the longer lifetime) and makes the backend the type
    `Veloce.security_audit` recognises, so `veloce check` warns when its session
    cookie is not `Secure`. A backend that does not subclass gets neither.

    A subclass sets `max_age` and `permanent_lifetime`, then calls
    `cookie_lifetime(session)` wherever it writes the cookie or the store entry.

    Usage::

        from veloce import SessionMiddlewareBase

        class RedisSessionMiddleware(SessionMiddlewareBase):
            def __init__(self, client, max_age=3600, permanent_lifetime=2592000):
                self.client = client
                self.max_age = max_age
                self.permanent_lifetime = permanent_lifetime

            async def process_response(self, request, response):
                session = request.session
                ttl = self.cookie_lifetime(session)
                await self.client.setex(session.sid, ttl, session.serialize())
                response.set_cookie("session", session.sid, max_age=ttl, secure=True)
                return response
    """

    #: Cookie lifetime, in seconds, for a session not marked permanent.
    max_age: int
    #: Cookie and store lifetime, in seconds, when `session.permanent` is set.
    permanent_lifetime: int
    #: Whether the cookie carries `Secure`; unset audits as insecure.
    secure: bool = False

    def cookie_lifetime(self, session: Any) -> int:
        """Return the lifetime this session's cookie and entry should carry."""
        return self.permanent_lifetime if getattr(session, "permanent", False) else self.max_age

    def _configure_cookie(
        self,
        *,
        cookie_name: str | _UnsetType,
        max_age: int,
        permanent_lifetime: int | _UnsetType,
        path: str | _UnsetType,
        httponly: bool | _UnsetType,
        secure: bool | _UnsetType,
        samesite: str | None | _UnsetType,
        domain: str | None,
        cookie_prefix: CookiePrefix | Literal["host", "secure"] | None,
        partitioned: bool,
        vary_on_cookie: bool,
        persist_on_status: Callable[[int], bool] | None,
        renew_on_access: bool,
    ) -> None:
        """Settle, validate and assign every cookie setting both backends share.

        Each `_UNSET` argument takes the library default here, so the object is
        fully formed at construction and a bad combination of settings fails
        there rather than on the first request. Only `secret_key` and `store`
        are left to the subclass; a third backend gets the validation and the
        `__Host-`/`__Secure-` wire name by calling this.
        """
        shared = _shared_cookie_settings(
            cookie_name=cookie_name,
            path=path,
            httponly=httponly,
            secure=secure,
            samesite=samesite,
        )
        # Rebound under their settled types, every `_UNSET` now replaced.
        settled_cookie_name: str = shared["cookie_name"]
        settled_path: str = shared["path"]
        settled_httponly: bool = shared["httponly"]
        settled_secure: bool = shared["secure"]
        settled_samesite: str | None = shared["samesite"]
        _validate_cookie_security(
            cookie_prefix=cookie_prefix,
            partitioned=partitioned,
            domain=domain,
            path=settled_path,
            secure=settled_secure,
            samesite=settled_samesite,
        )
        self.cookie_name = settled_cookie_name
        self.max_age = max_age
        # The `Max-Age` when `session.permanent` is set; 31 days in both backends.
        self.permanent_lifetime = (
            86400 * 31 if isinstance(permanent_lifetime, _UnsetType) else permanent_lifetime
        )
        self.path = settled_path
        self.httponly = settled_httponly
        self.secure = settled_secure
        self.samesite = settled_samesite
        self.domain = domain
        self.cookie_prefix: CookiePrefix | Literal["host", "secure"] | None = cookie_prefix
        self.partitioned = partitioned
        # Without `Vary: Cookie` a URL-keyed cache could serve one user's
        # session-bearing body to another (RFC 9110 Sec. 12.5.5).
        self.vary_on_cookie = vary_on_cookie
        # `None` means "status < 500": no half-mutated session on a failure.
        self._persist_on_status = persist_on_status
        # Sliding expiry: a session merely *read* has its lifetime refreshed.
        self.renew_on_access = renew_on_access
        # Read and write must share the prefixed wire name.
        self._wire_cookie_name = _wire_name(cookie_prefix, settled_cookie_name)

    @property
    def wire_cookie_name(self) -> str:
        """The cookie name actually used on the wire.

        `cookie_name` with the RFC 6265bis `__Host-` / `__Secure-` prefix
        applied. Anything reading or writing the cookie directly needs this name:
        under the bare one the cookie is never found and the read silently takes
        the anonymous path.
        """
        return self._wire_cookie_name

    def cookie_is_secure(self) -> bool:
        """Whether this backend's session cookie will carry `Secure`.

        A subclass that never sets `secure` reads as not secure, so the audit
        reports a cookie it cannot vouch for rather than staying quiet.
        """
        return bool(getattr(self, "secure", False))

    def _delete_cookie(self, response: Response, name: str, *, prefix: bool) -> None:
        """Tell the client to drop `name` using this middleware's attribute set.

        One place mapping the shared attribute set to `delete_cookie`, because a
        delete whose attributes disagree with the set leaves the cookie in place.

        `prefix` is True for the base cookie passed by its bare name; a chunk
        cookie passes its already-prefixed wire name with `prefix=False`.
        """
        response.delete_cookie(
            name,
            path=self.path,
            domain=self.domain,
            secure=self.secure,
            httponly=self.httponly,
            samesite=self.samesite,
            partitioned=self.partitioned,
            prefix=self.cookie_prefix if prefix else None,
        )

    def audit(self, ctx: AuditContext) -> Iterable[Finding]:
        """Report an insecure cookie, and config that no longer configures one."""
        stale = sorted(key for key in _RETIRED_CONFIG_KEYS if ctx.app.config.get(key) is not None)
        if stale:
            # Otherwise an app that set one silently loses its `Secure` cookie.
            yield Finding(
                f"{', '.join(stale)} no longer configures the session cookie - "
                f"{type(self).__name__} takes its cookie settings from its constructor.",
                severity="error",
                fix="pass "
                + ", ".join(f"{_RETIRED_CONFIG_KEYS[key]}=" for key in stale)
                + f" to {type(self).__name__}",
                id="session-config-retired",
            )
        if not self.cookie_is_secure():
            yield Finding(
                "The session cookie is not Secure - it can be sent over plain HTTP.",
                severity="warning",
                fix=f"pass secure=True to {type(self).__name__}",
                id="session-cookie-insecure",
            )
