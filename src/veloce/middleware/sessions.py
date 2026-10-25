"""Cookie-based session middleware — signed + timestamped payload.

The cookie body is produced by `veloce.signing.Signer`, which embeds a
server-side timestamp so `max_age` is enforced on the server: the cookie's own
`Max-Age` is a client hint an attacker ignores when replaying a stolen cookie,
so it is never the sole expiry signal.

Secret rotation: pass a list of secrets - the first signs new cookies, the rest
are accepted on read until the old cookies age out.
"""

from __future__ import annotations

import logging
import secrets
import warnings
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, Literal

from veloce._constants import HEADER_COOKIE
from veloce.audit import Finding
from veloce.http.cookies import dump_cookie
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware
from veloce.sessions import InMemorySessionStore, Session, SessionStore
from veloce.signing import BadSignature, Signer

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    from veloce.audit import AuditContext

# A literal, not `__name__`: "veloce.sessions" is what callers filter on.
_logger = logging.getLogger("veloce.sessions")


class _UnsetType:
    """Marks a constructor argument the caller left out."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


# `None` cannot be the marker: it is a meaningful `samesite` value. Narrow
# with `isinstance`, which mypy does not do for a plain singleton.
_UNSET: Final = _UnsetType()

# RFC 6265 Sec. 6.1 mandates 4096 bytes, but some implementations reserve 3.
_DEFAULT_MAX_COOKIE_SIZE = 4093

# Caps the Set-Cookie lines one chunked session can emit, so a huge session is
# dropped rather than exploded (RFC 6265 Sec. 6.1 caps per-domain cookies too).
_DEFAULT_MAX_COOKIE_CHUNKS = 8
# A literal `.` is a valid RFC 6265 token character.
_CHUNK_SEP = "."


def _validate_cookie_security(
    *,
    cookie_prefix: Literal["host", "secure"] | None,
    partitioned: bool,
    domain: str | None,
    path: str,
    secure: bool,
    samesite: str | None,
) -> None:
    """Fail-fast validation of the cookie security config at construction time.

    Enforces the RFC 6265bis name-prefix invariants and the CHIPS
    (`Partitioned`) preconditions at app wiring, where a misconfiguration
    surfaces instead of being silently dropped at response time.
    """
    if cookie_prefix is not None:
        if cookie_prefix not in ("host", "secure"):
            raise ValueError("cookie_prefix must be 'host', 'secure', or None")
        if not secure:
            raise ValueError(f"cookie_prefix={cookie_prefix!r} requires secure=True")
        if cookie_prefix == "host":
            if path != "/":
                raise ValueError("cookie_prefix='host' requires path='/'")
            if domain is not None:
                raise ValueError("cookie_prefix='host' requires domain=None")
    if partitioned and (not secure or (samesite or "").lower() != "none"):
        raise ValueError("partitioned=True (CHIPS) requires secure=True and samesite='none'")
    # Browsers drop a non-Secure SameSite=None cookie (RFC 6265bis same-site).
    if domain is not None and not secure and (samesite or "").lower() == "none":
        warnings.warn(
            "A SameSite=None cookie without Secure is rejected by modern "
            "browsers; a cross-subdomain Domain cookie set this way will be "
            "dropped. Set secure=True.",
            UserWarning,
            stacklevel=3,
        )


def _reassemble_chunks(cookies: Any, base_name: str, max_chunks: int) -> str | None:
    """Rebuild a chunked cookie value from `<base_name>.0`, `.1`, ... cookies.

    The chunks must be contiguous from index 0, and at most `max_chunks` indices
    are read so a forged cookie header cannot drive an unbounded scan. The signed
    value is one URL-safe token, so concatenation inverts the response-side split.
    """
    first = cookies.get(f"{base_name}{_CHUNK_SEP}0")
    if first is None:
        return None
    parts = [first]
    for index in range(1, max_chunks):
        part = cookies.get(f"{base_name}{_CHUNK_SEP}{index}")
        if part is None:
            break
        parts.append(part)
    return "".join(parts)


def _build_signer(secret_key: str | list[str]) -> Signer:
    """Build the session signer; a list rotates secrets (the first one signs)."""
    keys = [secret_key] if isinstance(secret_key, str) else list(secret_key)
    if not keys:
        raise ValueError("secret_key must be a non-empty string or list of strings")
    signer = Signer(keys[0], salt="veloce.session")
    for fallback in keys[1:]:
        signer.add_fallback_secret(fallback, salt="veloce.session")
    return signer


def _wire_name(cookie_prefix: Literal["host", "secure"] | None, cookie_name: str) -> str:
    """Apply the RFC 6265bis name prefix to the configured cookie name.

    The prefix must be on both the request read and the response write, so it is
    derived once at construction and the per-request read stays off the hot path.
    """
    if cookie_prefix == "host":
        return f"__Host-{cookie_name}"
    if cookie_prefix == "secure":
        return f"__Secure-{cookie_name}"
    return cookie_name


def _should_persist(policy: Callable[[int], bool] | None, status_code: int) -> bool:
    """Whether a modified session should be written for this response status.

    The `None` policy default means "persist for status < 500": a failed request
    should not write a half-mutated session.
    """
    return policy(status_code) if policy is not None else status_code < 500


def _begin_session_response(
    vary_on_cookie: bool, session: Any, response: Response
) -> tuple[bool, bool] | None:
    """Shared response-phase preamble for both session middlewares.

    Returns `None` when there is no session to persist, else `(accessed,
    modified)` after emitting `Vary: Cookie` when the handler touched the
    session - without which a URL-keyed cache could share one user's
    session-personalized body with another (RFC 9110 Sec. 12.5.5).
    """
    if session is None:
        return None
    accessed = getattr(session, "accessed", False)
    modified = getattr(session, "modified", False)
    if vary_on_cookie and (accessed or modified):
        response.add_vary(HEADER_COOKIE)
    return accessed, modified


# In one place, so a new shared setting is wired once not per `__init__`.
_SHARED_COOKIE_DEFAULTS: dict[str, Any] = {
    "cookie_name": "session",
    "path": "/",
    "httponly": True,
    "secure": False,
    "samesite": "lax",
}


def _shared_cookie_settings(**supplied: Any) -> dict[str, Any]:
    """Settle the shared cookie settings from the constructor and the defaults.

    The constructor is the only source: a setting the caller did not pass takes
    the library default here and does not change again, so two middlewares can
    carry different cookies.
    """
    return {
        name: _SHARED_COOKIE_DEFAULTS[name] if isinstance(value, _UnsetType) else value
        for name, value in supplied.items()
    }


# Kept only to recognise a configuration that no longer configures anything.
_RETIRED_CONFIG_KEYS: dict[str, str] = {
    "APPLICATION_ROOT": "path",
    "MAX_COOKIE_SIZE": "max_cookie_size",
    "PERMANENT_SESSION_LIFETIME": "permanent_lifetime",
    "SESSION_COOKIE_HTTPONLY": "httponly",
    "SESSION_COOKIE_NAME": "cookie_name",
    "SESSION_COOKIE_SAMESITE": "samesite",
    "SESSION_COOKIE_SECURE": "secure",
}


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
        cookie_prefix: Literal["host", "secure"] | None,
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
        self.cookie_prefix: Literal["host", "secure"] | None = cookie_prefix
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


class SessionMiddleware(SessionMiddlewareBase):
    """Server-side session stored in a signed, timestamped cookie.

    Every cookie setting comes from this constructor; an argument left out takes
    the library default and does not change again, since `app.config` is not
    consulted. `secret_key` is the exception: left out, it is taken from
    `SECRET_KEY` (also settable as `app.secret_key`) on the first request,
    because it is the application's signing key rather than an attribute of this
    cookie. Without either, the first request raises.

    Set `renew_on_access=True` for sliding expiry: a session that was only read
    has its cookie re-signed with a fresh `Max-Age`, so an active user is not
    logged out at the fixed `max_age`. Default off.

    Set `chunked=True` to split a signed value larger than `max_cookie_size`
    across numbered cookies (`<cookie_name>.0`, `.1`, ...) and reassemble them on
    the next request, bounded by `max_chunks`. Default off, in which case the
    oversized cookie is dropped with a warning.
    """

    def __init__(
        self,
        secret_key: str | list[str] | None = None,
        cookie_name: str | _UnsetType = _UNSET,
        max_age: int = 86400 * 14,
        path: str | _UnsetType = _UNSET,
        httponly: bool | _UnsetType = _UNSET,
        secure: bool | _UnsetType = _UNSET,
        samesite: str | None | _UnsetType = _UNSET,
        domain: str | None = None,
        permanent_lifetime: int | _UnsetType = _UNSET,
        max_cookie_size: int | _UnsetType = _UNSET,
        vary_on_cookie: bool = True,
        persist_on_status: Callable[[int], bool] | None = None,
        cookie_prefix: Literal["host", "secure"] | None = None,
        partitioned: bool = False,
        renew_on_access: bool = False,
        chunked: bool = False,
        max_chunks: int = _DEFAULT_MAX_COOKIE_CHUNKS,
        name: str | None = None,
    ) -> None:
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
        # The application's key, not an attribute of this cookie.
        self._pending_config = secret_key is None
        if secret_key is not None:
            self._signer = _build_signer(secret_key)
        if isinstance(max_cookie_size, _UnsetType):
            max_cookie_size = _DEFAULT_MAX_COOKIE_SIZE
        self.max_cookie_size = max_cookie_size
        # `max_chunks` bounds the split, so a huge session is dropped instead.
        if max_chunks < 1:
            raise ValueError("max_chunks must be >= 1")
        self.chunked = chunked
        self.max_chunks = max_chunks

    def audit(self, ctx: AuditContext) -> Iterable[Finding]:
        """Report a backend that cannot sign, on top of the shared checks.

        A middleware with neither a `secret_key=` argument nor a configured
        `SECRET_KEY` raises on its first request, so this is an `error` that
        refuses the boot rather than a warning discovered by traffic. The
        middleware answers it because only it knows what the constructor got.
        """
        yield from super().audit(ctx)
        if self._pending_config and not ctx.app.config.get("SECRET_KEY"):
            yield Finding(
                f"{type(self).__name__} has no signing key, so it cannot sign a "
                "session cookie and every request through it fails.",
                severity="error",
                fix="pass secret_key= to the middleware, or set app.secret_key",
                id="session-secret-key-missing",
            )

    def _resolve_config(self, request: Request) -> None:
        """Take the signing key from the app when the constructor was not given one.

        Runs once, on the first request. Cookie settings are not touched: they
        were settled at construction and the app's config does not configure
        them.
        """
        app = request.app
        self.bind_secret_key(
            app.config if app is not None else {},
            hint="before the first request",
        )

    def bind_secret_key(self, config: Mapping[str, Any], *, hint: str = "") -> None:
        """Settle the signing key from `config` if the constructor was not given one.

        A no-op once the key is settled, so it is safe to call from anywhere
        that needs a usable signer before a request has run - seeding a session
        in a test, or a CLI command that mints one. `hint` is appended to the
        error raised when there is no key, so the caller can say when it needed
        it.
        """
        if not self._pending_config:
            return
        secret = config.get("SECRET_KEY")
        if not secret:
            raise RuntimeError(
                "SessionMiddleware has no secret key - pass secret_key= at "
                "construction or set app.secret_key (config['SECRET_KEY'])"
                + (f" {hint}." if hint else ".")
            )
        self._signer = _build_signer(secret)
        self._pending_config = False

    # ── The cookie value, for anything that is not a request ──

    def _require_signer(self, caller: str) -> None:
        """Refuse clearly when the signing key has not been settled yet.

        A middleware constructed without `secret_key=` takes the app's
        `SECRET_KEY` on the first request. Called before that, these two would
        have raised `AttributeError` on a private attribute, which says nothing
        about what to do.
        """
        if self._pending_config:
            raise RuntimeError(
                f"{caller}() has no signing key yet - pass secret_key= at "
                f"construction, or call bind_secret_key(app.config) first."
            )

    def encode_cookie(self, session: Mapping[str, Any]) -> str:
        """Return the signed cookie value this middleware would send for `session`.

        Minting a valid session cookie outside a request - a test fixture that
        starts logged in, a tool that hands a user a pre-authenticated link -
        otherwise means rebuilding the signer with this middleware's exact
        secret and salt, and any drift in that construction produces cookies the
        middleware rejects.
        """
        self._require_signer("encode_cookie")
        return self._signer.dumps(dict(session))

    def decode_cookie(self, value: str) -> dict[str, Any] | None:
        """Return the session inside a signed cookie value, or `None` if invalid.

        `None` covers a bad signature, a tampered payload, and a token older than
        this middleware accepts. The request path calls this, so the two cannot
        diverge.
        """
        self._require_signer("decode_cookie")
        try:
            # The longer window first, so a permanent cookie is not rejected
            # before its flag is known - its own `Max-Age` being a client hint
            # an attacker ignores when replaying (RFC 6265 Sec. 5.3).
            lenient = max(self.max_age, self.permanent_lifetime)
            decoded = self._signer.loads(value, max_age=lenient)
            # Then the flag-appropriate ceiling, so neither a stolen
            # non-permanent cookie nor a stale permanent one replays past its own
            # limit. A tampered flag cannot help: the payload is HMAC-signed.
            if isinstance(decoded, dict):
                ceiling = (
                    self.permanent_lifetime if decoded.get("_permanent", False) else self.max_age
                )
                if ceiling < lenient:
                    decoded = self._signer.loads(value, max_age=ceiling)
        except BadSignature:
            return None
        return decoded if isinstance(decoded, dict) else None

    async def process_request(self, request: Request) -> Response | None:
        """Load the session from the signed cookie into request state."""
        if self._pending_config:
            self._resolve_config(request)
        session_data: dict[str, Any] = {}
        is_new = True
        cookie_val = request.cookies.get(self._wire_cookie_name)
        # A whole cookie wins, so stale chunks on the client cannot shadow it.
        if not cookie_val and self.chunked:
            cookie_val = _reassemble_chunks(
                request.cookies, self._wire_cookie_name, self.max_chunks
            )
        if cookie_val:
            decoded = self.decode_cookie(cookie_val)
            if decoded is not None:
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
        begun = _begin_session_response(self.vary_on_cookie, session, response)
        # No session attached (handler bypassed middleware?) -> nothing to do.
        if begun is None:
            return response
        accessed, modified = begun

        # `renew_on_access` re-signs a session that was only *read*, sliding
        # its timestamp and `Max-Age` forward.
        if not modified and not (
            self.renew_on_access and accessed and not getattr(session, "new", False) and session
        ):
            return response

        # A 5xx should persist neither a Set-Cookie nor the empty-session delete.
        if not _should_persist(self._persist_on_status, response.status_code):
            return response

        if not session:
            self._delete_cookie(response, self.cookie_name, prefix=True)
            # Or an emptied session resurrects from stale `<name>.N` parts.
            if self.chunked:
                self._clear_chunks(response)
            return response

        cookie_value = self.encode_cookie(session)
        lifetime = self.cookie_lifetime(session)
        # Browsers silently truncate Set-Cookie above ~4 KB (RFC 6265 Sec. 6.1),
        # corrupting the session. Dropped, not raised: a raise here would
        # re-enter this middleware through the error-response path.
        rendered = self._render_cookie(self.cookie_name, cookie_value, lifetime, prefix=True)
        rendered_size = self._rendered_size(rendered)
        # It fits, so stale chunks go and the client keeps one encoding.
        if rendered_size <= self.max_cookie_size:
            response._append_set_cookie_header(rendered)
            response._encoded = None
            if self.chunked:
                self._clear_chunks(response)
            return response

        # Too large for one cookie, so split it rather than drop it.
        if self.chunked:
            if self._write_chunks(response, cookie_value, lifetime):
                return response
            # Over `max_chunks` even chunked, so every slot is cleared rather
            # than leaving a smaller session to resurrect.
            self._delete_cookie(response, self.cookie_name, prefix=True)
            self._clear_chunks(response)
            _logger.warning(
                "Session cookie %r needs more than max_chunks=%d chunks; "
                "dropping Set-Cookie on this response. Switch to "
                "ServerSessionMiddleware for payloads of this size.",
                self.cookie_name,
                self.max_chunks,
            )
            return response

        # Oversized with chunking off, so dropped (RFC 6265 Sec. 6.1).
        _logger.warning(
            "Session cookie %r is %d bytes, exceeds max_cookie_size=%d; "
            "dropping Set-Cookie on this response. Enable chunked=True or "
            "switch to ServerSessionMiddleware for payloads of this size.",
            self.cookie_name,
            rendered_size,
            self.max_cookie_size,
        )
        return response

    def _chunk_name(self, index: int) -> str:
        """Bare wire name of chunk `index`, e.g. `__Host-session.0`.

        Built on the prefixed wire name, so the `__Host-`/`__Secure-` guarantees
        carry to every chunk. The prefix stays in the literal name because
        `dump_cookie` would otherwise re-derive `__Host-__Host-...`.
        """
        return f"{self._wire_cookie_name}{_CHUNK_SEP}{index}"

    def _render_cookie(self, name: str, value: str, lifetime: int, *, prefix: bool) -> str:
        """Serialise one Set-Cookie line from this middleware's attributes.

        `Response.set_cookie` cannot serve here because `_rendered_size` needs
        the serialised line before it is attached to a response, so the CHIPS
        attribute is appended below instead. `prefix` is True only for the base
        cookie passed by its bare `cookie_name`.
        """
        rendered = dump_cookie(
            name,
            value,
            max_age=lifetime,
            path=self.path,
            domain=self.domain,
            httponly=self.httponly,
            secure=self.secure,
            samesite=self.samesite,
            prefix=self.cookie_prefix if prefix else None,
        )
        # Browsers reject `Partitioned` without `Secure`, costing the cookie.
        if self.partitioned and self.secure:
            rendered += "; Partitioned"
        return rendered

    @staticmethod
    def _rendered_size(rendered: str) -> int:
        """On-the-wire byte length of a Set-Cookie line.

        Bytes, not characters: a non-ASCII cookie_name/path/domain would
        under-count and let the line pass the browser's ~4 KB truncation limit.
        An all-ASCII line needs no encode, and the latin-1 fallback still raises
        above U+00FF.
        """
        return len(rendered) if rendered.isascii() else len(rendered.encode("latin-1"))

    def _write_chunks(self, response: Response, value: str, lifetime: int) -> bool:
        """Split `value` across numbered cookies `<name>.0`, `.1`, ... .

        Returns True when the value fit within `max_chunks`, else False and the
        caller drops it with a warning. The per-chunk budget is the limit minus
        an empty chunk cookie's rendered overhead; the signed value is a URL-safe
        token, so one token char renders as one byte. Built into a scratch list
        first, so a too-large value writes no partial cookies, and on success the
        base cookie and stale higher chunks are cleared.
        """
        chunks: list[str] = []
        start = 0
        index = 0
        length = len(value)
        while start < length:
            if index >= self.max_chunks:
                return False
            name = self._chunk_name(index)
            # A multi-digit index widens the name, so index 0's budget is wrong.
            piece_overhead = self._rendered_size(
                self._render_cookie(name, "", lifetime, prefix=False)
            )
            piece_budget = self.max_cookie_size - piece_overhead
            if piece_budget < 1:
                return False
            piece = value[start : start + piece_budget]
            chunks.append(self._render_cookie(name, piece, lifetime, prefix=False))
            start += piece_budget
            index += 1
        for rendered in chunks:
            response._append_set_cookie_header(rendered)
        # The base cookie and higher chunks may hold stale sessions.
        self._delete_cookie(response, self.cookie_name, prefix=True)
        for stale in range(index, self.max_chunks):
            self._delete_cookie(response, self._chunk_name(stale), prefix=False)
        response._encoded = None
        return True

    def _clear_chunks(self, response: Response) -> None:
        """Delete every possible chunk cookie (`<name>.0` .. `<name>.N-1`)."""
        for index in range(self.max_chunks):
            self._delete_cookie(response, self._chunk_name(index), prefix=False)


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
