"""Cookie-based session middleware — signed + timestamped payload.

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

# The logger name is part of the public contract: callers (and the test
# suite) filter on "veloce.sessions", so it stays a literal rather than
# `__name__` (which would resolve to "veloce.middleware.sessions").
_logger = logging.getLogger("veloce.sessions")


class _UnsetType:
    """Marks a constructor argument the caller left out."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


# Marks a constructor argument the caller left out, so the library default
# applies. `None` cannot serve as the marker - it is a meaningful value for
# several settings (e.g. `samesite`). It carries its own type rather than `Any`
# so a parameter defaulting to it declares `str | _UnsetType` and the checker
# still sees the settings path; narrow with `isinstance(x, _UnsetType)`, since
# mypy does not narrow an identity test against a plain singleton.
_UNSET: Final = _UnsetType()

# RFC 6265 Sec. 6.1 only mandates 4096 bytes per cookie (name + value + attrs);
# browsers and proxies enforce this inconsistently, so 4093 is the de-facto
# safe ceiling (4096 - 3 bytes of separator overhead some impls reserve).
_DEFAULT_MAX_COOKIE_SIZE = 4093

# Chunked-cookie defaults. When `chunked=True`, a signed value too large for a
# single cookie is split across numbered cookies named `<name>.0`, `<name>.1`,
# ... up to `_DEFAULT_MAX_COOKIE_CHUNKS`. The cap bounds how many Set-Cookie
# lines one session can emit (RFC 6265 Sec. 6.1 also caps per-domain cookies),
# so an absurdly large session is dropped with a warning rather than flooding
# the response with hundreds of cookies.
_DEFAULT_MAX_COOKIE_CHUNKS = 8
# Separator between the base cookie name and the chunk index. A literal `.` is
# a valid RFC 6265 token character and is widely used for this convention.
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

    Enforces, once for both middlewares, the RFC 6265bis name-prefix invariants
    and the CHIPS (`Partitioned`) preconditions so a misconfiguration surfaces
    at app wiring rather than being silently dropped at response time.
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
    # A non-Secure SameSite=None cross-subdomain cookie is dropped by modern
    # browsers (RFC 6265bis same-site rules / Chrome). Warn at construction.
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

    The chunks must be contiguous from index 0: the first missing index ends the
    sequence. Returns the concatenated value, or None when no `.0` chunk exists.
    Reads at most `max_chunks` indices so a forged cookie header cannot drive an
    unbounded scan. The signed value is a single URL-safe token, so plain string
    concatenation is the exact inverse of the response-side split.
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

    The wire name carries the `__Host-`/`__Secure-` prefix so the request read
    and the response write agree on the same name. Derived once at construction
    to keep the per-request read off the hot path. Shared by both middlewares.
    """
    if cookie_prefix == "host":
        return f"__Host-{cookie_name}"
    if cookie_prefix == "secure":
        return f"__Secure-{cookie_name}"
    return cookie_name


def _should_persist(policy: Callable[[int], bool] | None, status_code: int) -> bool:
    """Whether a modified session should be written for this response status.

    The `None` policy default means "persist for status < 500" - a failed
    request should not write a half-mutated session. Shared by both middlewares.
    """
    return policy(status_code) if policy is not None else status_code < 500


def _begin_session_response(
    vary_on_cookie: bool, session: Any, response: Response
) -> tuple[bool, bool] | None:
    """Shared response-phase preamble for both session middlewares.

    Returns `None` when there is no session to persist (the caller returns the
    response unchanged). Otherwise returns `(accessed, modified)` after emitting
    `Vary: Cookie` when the handler touched the session, so a URL-keyed cache
    cannot share a session-personalized body across users (RFC 9110 Sec.
    12.5.5). `getattr` with a default tolerates a non-`Session` object placed
    under the reserved `session` state key, skipping the session work as before.
    """
    if session is None:
        return None
    accessed = getattr(session, "accessed", False)
    modified = getattr(session, "modified", False)
    if vary_on_cookie and (accessed or modified):
        response.add_vary(HEADER_COOKIE)
    return accessed, modified


# The cookie settings both session middlewares take, and the default each uses
# when the constructor is not given one. Held here rather than spelled out in
# each `__init__` so a new shared setting is wired in one place - the two copies
# had already diverged in how they render SameSite.
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
    the library default here and does not change again. Nothing re-reads the
    app's config later, so a middleware is fully configured the moment it is
    built and two middlewares can carry different cookies.
    """
    return {
        name: _SHARED_COOKIE_DEFAULTS[name] if isinstance(value, _UnsetType) else value
        for name, value in supplied.items()
    }


# Config keys that configured the session cookie before the constructor became
# its only source. Kept only to recognise a stale configuration and say so.
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

    Subclassing supplies two things a backend would otherwise have to
    reimplement. It inherits the documented `session.permanent` rule (a
    permanent session takes the longer lifetime), and it becomes the type
    `Veloce.security_audit` recognises, so `veloce check` warns when the
    backend's session cookie is not `Secure`. A backend that does not
    subclass gets neither, and the audit passes it in silence.

    A subclass sets `max_age` and `permanent_lifetime`, then calls
    `cookie_lifetime(session)` wherever it writes the cookie or the store
    entry. Both built-in backends - `SessionMiddleware` (signed cookie) and
    `ServerSessionMiddleware` (server-side store) - are subclasses, and
    adding another requires no edit inside the framework.

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
    #: Cookie and store lifetime, in seconds, for a session marked
    #: `session.permanent`.
    permanent_lifetime: int
    #: Whether the session cookie carries `Secure`. A subclass that does not
    #: set it is audited as insecure.
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
        fully formed at construction and a bad combination of settings fails at
        that point rather than on the first request. Only `secret_key` and
        `store` are left to the subclass - they are what distinguishes the
        backends; everything above this line is the cookie, and the cookie is
        the same.

        A subclass adding a third backend gets the validation and the
        `__Host-`/`__Secure-` wire name by calling this, rather than by copying
        forty lines that must then be kept in step.
        """
        shared = _shared_cookie_settings(
            cookie_name=cookie_name,
            path=path,
            httponly=httponly,
            secure=secure,
            samesite=samesite,
        )
        # Rebound under their settled types: `_shared_cookie_settings` has
        # replaced every `_UNSET` with the library default, so from here down
        # these are the plain values the attributes below - and every reader of
        # them - are typed as.
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
        # `PERMANENT_SESSION_LIFETIME` analog - the cookie `Max-Age` when
        # `session.permanent` is set. Both backends default to 31 days, so the
        # two answer `session.permanent = True` identically.
        self.permanent_lifetime = (
            86400 * 31 if isinstance(permanent_lifetime, _UnsetType) else permanent_lifetime
        )
        self.path = settled_path
        self.httponly = settled_httponly
        self.secure = settled_secure
        self.samesite = settled_samesite
        self.domain = domain
        self.cookie_prefix = cookie_prefix
        self.partitioned = partitioned
        # Emit `Vary: Cookie` on responses that write the session cookie, so a
        # shared cache keyed on URL alone can't serve one user's session-bearing
        # body to another (RFC 9110 Sec. 12.5.5). Set False to opt out.
        self.vary_on_cookie = vary_on_cookie
        # Policy deciding whether to persist for a given response status. The
        # `None` default means "persist for status < 500" - a failed request
        # should not write a half-mutated session.
        self._persist_on_status = persist_on_status
        # Sliding expiry: when True, a session merely *read* during the request
        # has its lifetime refreshed on the way out, so an active user is never
        # logged out mid-session. Default False keeps the prior behaviour.
        self.renew_on_access = renew_on_access
        # Read and write must share the prefixed wire name.
        self._wire_cookie_name = _wire_name(cookie_prefix, settled_cookie_name)

    @property
    def wire_cookie_name(self) -> str:
        """The cookie name actually used on the wire.

        `cookie_name` with the RFC 6265bis `__Host-` / `__Secure-` prefix
        applied. Anything writing or reading this backend's cookie directly
        needs this name and not the configured one - seeded under the bare name
        the cookie is simply never found, and the read silently takes the
        anonymous path.
        """
        return self._wire_cookie_name

    def cookie_is_secure(self) -> bool:
        """Whether this backend's session cookie will carry `Secure`.

        Answered from the middleware alone - there is no second source to
        reconcile. A subclass that never sets `secure` reads as not secure, so
        the audit reports a cookie it cannot vouch for rather than staying
        quiet about it.
        """
        return bool(getattr(self, "secure", False))

    def _delete_cookie(self, response: Response, name: str, *, prefix: bool) -> None:
        """Tell the client to drop `name` using this middleware's attribute set.

        The single place mapping the shared cookie attribute set to
        `delete_cookie`, for the same reason `_configure_cookie` is the single
        place that settles it: a per-backend copy has already diverged once (see
        the SameSite note above), and a delete whose attributes disagree with
        the set leaves the cookie in place.

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
            # These keys used to configure the cookie. An app that set one and
            # passed no constructor argument had a `Secure` cookie and would now
            # silently have a plain one, so the boot stops instead: a security
            # setting must not quietly weaken because its home moved.
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

    Every cookie setting comes from this constructor. An argument left out
    takes the library default and does not change again - `app.config` is not
    consulted, so what the signature says is what the cookie carries, and two
    session middlewares can carry different cookies.

    `secret_key` is the exception: left out, it is taken from `SECRET_KEY`
    (also settable as `app.secret_key`) on the first request, because it is the
    application's signing key rather than an attribute of this cookie. Without
    either, the first request raises.

    Set `renew_on_access=True` for sliding expiry: a session that was only read
    during a request has its cookie re-signed with a fresh `Max-Age` on the way
    out, so an active user is not logged out at the fixed `max_age`. Default is
    off - only a modifying write rewrites the cookie.

    Set `chunked=True` to transparently split a signed value larger than
    `max_cookie_size` across numbered cookies (`<cookie_name>.0`, `.1`, ...) and
    reassemble them on the next request. `max_chunks` bounds the split so an
    oversized session is dropped with a warning rather than exploded into an
    unbounded number of cookies. Default is off - the single oversized cookie is
    dropped with a warning, unchanged from before.
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
        # The signing key is the one setting still settled against the app: it
        # is the application's key, not an attribute of this cookie, and
        # `app.secret_key` is already its single home.
        self._pending_config = secret_key is None
        if secret_key is not None:
            self._signer = _build_signer(secret_key)
        if isinstance(max_cookie_size, _UnsetType):
            max_cookie_size = _DEFAULT_MAX_COOKIE_SIZE
        self.max_cookie_size = max_cookie_size
        # Opt-in transparent chunking: when True, a signed value too large for a
        # single cookie is split across numbered cookies (`<name>.0`, `.1`, ...)
        # on the response and transparently reassembled on the request. Default
        # off preserves the drop-with-warning behavior. `max_chunks` bounds the
        # split so an absurdly large session is dropped, not exploded into
        # hundreds of cookies.
        if max_chunks < 1:
            raise ValueError("max_chunks must be >= 1")
        self.chunked = chunked
        self.max_chunks = max_chunks

    def audit(self, ctx: AuditContext) -> Iterable[Finding]:
        """Report a backend that cannot sign, on top of the shared checks.

        A cookie session is signed, so a middleware with neither a
        `secret_key=` argument nor a configured `SECRET_KEY` cannot serve a
        single request - it raises on the first one. That is an `error`, which
        refuses the boot, rather than a warning discovered by traffic.

        The middleware answers this and not the audit, because only it knows
        whether the constructor supplied a key: reading `SECRET_KEY` alone
        warned about a backend that was already signing correctly.
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

        `None` covers a bad signature, a tampered payload, and a token older
        than this middleware would accept. The request path calls this, so what
        it accepts and what a request accepts cannot diverge.
        """
        self._require_signer("decode_cookie")
        try:
            # Decode with the longer window so a permanent cookie is not
            # rejected before its flag is known. The cookie's own `Max-Age`
            # is a client hint an attacker ignores when replaying a stolen
            # cookie (RFC 6265 Sec. 5.3), so the server-side ceiling is
            # re-checked against the payload's own `_permanent` flag.
            lenient = max(self.max_age, self.permanent_lifetime)
            decoded = self._signer.loads(value, max_age=lenient)
            # Enforce the flag-appropriate ceiling: a permanent session may
            # live for `permanent_lifetime`, a non-permanent one only for
            # `max_age` - independent of which value is configured larger.
            # When that ceiling is shorter than the lenient decode window,
            # re-validate the token's age against it, so neither a stolen
            # non-permanent cookie nor a stale permanent one replays past its
            # own limit. A tampered flag cannot help - the whole payload is
            # HMAC-signed.
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
        # When chunking is enabled and no single cookie is present, fall back to
        # reassembling the numbered chunks. A whole (unchunked) cookie always
        # wins so a session that later fits in one cookie reads correctly even
        # while stale chunks linger before their delete reaches the client.
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

        # The re-sign + Set-Cookie is normally skipped when the session was never
        # mutated. With `renew_on_access`, an existing session that was only
        # *read* (accessed, non-empty, not new) is also re-signed so its
        # server-side timestamp and `Max-Age` slide forward - the idle reset.
        if not modified and not (
            self.renew_on_access and accessed and not getattr(session, "new", False) and session
        ):
            return response

        # A 5xx response should not persist a half-mutated session - neither a
        # Set-Cookie nor the empty-session delete. The gate wraps the whole
        # persist block. Default (no policy): persist only for status < 500.
        if not _should_persist(self._persist_on_status, response.status_code):
            return response

        if not session:
            self._delete_cookie(response, self.cookie_name, prefix=True)
            # Clear any chunk cookies a previous larger session left behind, so
            # an emptied session does not resurrect from stale `<name>.N` parts.
            if self.chunked:
                self._clear_chunks(response)
            return response

        cookie_value = self.encode_cookie(session)
        lifetime = self.cookie_lifetime(session)
        # Browsers silently truncate Set-Cookie above ~4 KB, which corrupts
        # the session on the next request. Measure the rendered header and
        # drop the cookie (with a warning) rather than raising - a raise here
        # re-enters this middleware via the error-response path and would
        # propagate as an unhandled ASGI exception. RFC 6265 Sec. 6.1.
        rendered = self._render_cookie(self.cookie_name, cookie_value, lifetime, prefix=True)
        rendered_size = self._rendered_size(rendered)
        # The single cookie fits: write it, and (chunked mode) clear any chunk
        # cookies a previous oversized response wrote so the client does not
        # keep two encodings of the same session.
        if rendered_size <= self.max_cookie_size:
            response._append_set_cookie_header(rendered)
            response._encoded = None
            if self.chunked:
                self._clear_chunks(response)
            return response

        # Too large for one cookie. With chunking enabled, split the signed
        # value across numbered cookies instead of dropping it.
        if self.chunked:
            if self._write_chunks(response, cookie_value, lifetime):
                return response
            # Fell through: even chunked, the value needs more than `max_chunks`
            # cookies. Drop with a warning rather than emit a partial session.
            # Clear the base cookie and every chunk slot so a previously-persisted
            # (smaller) session is not silently resurrected from the client's
            # stale cookies on the next request.
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

        # Non-chunked mode (the default): the single cookie is oversized and
        # chunking is off, so drop it with a warning. RFC 6265 Sec. 6.1.
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

        Built on the prefixed wire name so request reassembly and response
        writes agree, and so the `__Host-`/`__Secure-` guarantees carry to every
        chunk. The prefix lives in the literal name (not passed to `dump_cookie`)
        because `dump_cookie` would re-derive `__Host-__Host-...`.
        """
        return f"{self._wire_cookie_name}{_CHUNK_SEP}{index}"

    def _render_cookie(self, name: str, value: str, lifetime: int, *, prefix: bool) -> str:
        """Serialise one Set-Cookie line from this middleware's attributes.

        `dump_cookie` has no `partitioned` arg, so the CHIPS attribute is
        appended here rather than by `Response.set_cookie`, which this method
        cannot use because `_rendered_size` needs the serialised line before it
        is attached to a response. The `partitioned and secure` condition below
        is therefore `set_cookie`'s, restated. `prefix` is True only for the
        base cookie passed by its bare `cookie_name`; chunk cookies pass their
        already-prefixed wire name with `prefix=False`.
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
        # Mirrors `Response.set_cookie`: CHIPS keys a partitioned cookie to the
        # top-level site and browsers reject the attribute without `Secure`, so
        # emitting it alone would cost the whole cookie. The constructor guard
        # already rejects that combination; this keeps the two renderers
        # answering alike if either guard moves.
        if self.partitioned and self.secure:
            rendered += "; Partitioned"
        return rendered

    @staticmethod
    def _rendered_size(rendered: str) -> int:
        """On-the-wire byte length of a Set-Cookie line.

        Measure bytes, not characters: a non-ASCII cookie_name/path/domain would
        otherwise under-count and let the line exceed the browser's ~4 KB
        truncation limit. Cookie headers serialise as latin-1 (one byte per code
        point), so an all-ASCII line (the common case - the signed value is
        base64) needs no encode; the latin-1 fallback still raises on code
        points above U+00FF.
        """
        return len(rendered) if rendered.isascii() else len(rendered.encode("latin-1"))

    def _write_chunks(self, response: Response, value: str, lifetime: int) -> bool:
        """Split `value` across numbered cookies `<name>.0`, `.1`, ... .

        Returns True when the whole value fit within `max_chunks`, False when it
        needs more (the caller then drops with a warning). The per-chunk value
        budget is the limit minus the rendered overhead of an empty chunk
        cookie; the signed value is a URL-safe token, so it is not
        percent-expanded and one token char renders as one byte. Built into a
        scratch list first so a too-large value writes no partial cookies. On
        success the base cookie and any higher-numbered stale chunks are cleared
        so the client keeps exactly one encoding.
        """
        chunks: list[str] = []
        start = 0
        index = 0
        length = len(value)
        while start < length:
            if index >= self.max_chunks:
                return False
            name = self._chunk_name(index)
            # A multi-digit chunk index widens the name, so recompute the budget
            # per chunk rather than reusing index 0's.
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
        # Clear the base cookie (it may hold a stale smaller session) and any
        # higher-numbered stale chunks left by a previous larger session.
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

    The session payload lives in a `SessionStore`, not in the cookie, so a
    session is *revocable*: empty it in a handler (`session.clear()`) or
    delete it straight from the store (`await store.delete(session_id)`)
    and it is gone server-side. A tampered or stale cookie simply fails to
    resolve to a stored payload and is treated as a fresh session.

    The default store is a process-local `InMemorySessionStore`; pass a
    shared backend (e.g. a Redis-backed `SessionStore`) for a multi-worker
    deployment. The store is a plain object the caller owns - keep a
    reference to it to revoke sessions by id.

    Set `renew_on_access=True` for sliding expiry: a session that was only read
    during a request has its store TTL refreshed (via `SessionStore.touch`) and
    its cookie re-stamped on the way out - an idle-timeout reset. Default off.
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
        begun = _begin_session_response(self.vary_on_cookie, session, response)
        if begun is None:
            return response
        accessed, modified = begun

        if not modified:
            # Sliding expiry: an existing session that was only read (accessed,
            # not new) has its store TTL and cookie `Max-Age` refreshed, then
            # we are done - the payload is unchanged so there is no store write.
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
        # One lifetime for the entry and its cookie, so a `permanent` session
        # does not outlive its store entry or vice versa.
        lifetime = self.cookie_lifetime(session)
        if not session:
            # The handler emptied the session - revoke it server-side and
            # tell the client to drop the cookie.
            if session_id is not None:
                await self.store.delete(session_id)
                self._delete_cookie(response, self.cookie_name, prefix=True)
            return response

        if session_id is None or session.regenerate:
            # A fresh session, or an explicit id rotation requested via
            # `session.regenerate_id()` at a privilege boundary. Drop the
            # old store entry so the previous id can no longer resolve.
            if session_id is not None and session.regenerate:
                await self.store.delete(session_id)
            # Mint an unguessable id - 256 bits of entropy - and create it.
            session_id = secrets.token_urlsafe(32)
            await self.store.write(session_id, dict(session), lifetime)
        else:
            # An already-stored session: write back only if it still
            # exists. A concurrent request may have revoked it (logout,
            # `store.delete(...)`) while this one was in flight - a plain
            # `write` would resurrect it, so use the conditional `replace`.
            if not await self.store.replace(session_id, dict(session), lifetime):
                # Revoked under us - honour the revocation and drop the cookie.
                self._delete_cookie(response, self.cookie_name, prefix=True)
                return response
        self._set_session_cookie(response, session_id, lifetime)
        return response

    async def _renew(self, request: Request, response: Response) -> None:
        """Slide the store TTL and cookie `Max-Age` for a read-only access.

        Refreshes the existing entry's expiry without rewriting its payload.
        If the entry was revoked under us (`touch` returns False) the cookie is
        cleared, mirroring the revoked-under-us handling in `process_response`.
        """
        session_id = request._state.get("_session_id")
        # No stored id means the cookie did not resolve to an entry on read;
        # there is nothing to slide forward.
        if session_id is None:
            return
        # Slide by the lifetime this session is entitled to, so a permanent one
        # is not quietly demoted to the default window on a read-only request.
        lifetime = self.cookie_lifetime(request._state.get("session"))
        if not await self.store.touch(session_id, lifetime):
            self._delete_cookie(response, self.cookie_name, prefix=True)
            return
        self._set_session_cookie(response, session_id, lifetime)

    def _set_session_cookie(self, response: Response, session_id: str, lifetime: int) -> None:
        """Write the opaque session-id cookie with this middleware's attributes.

        Single place mapping the cookie attribute set to `set_cookie`, mirroring
        the base class's delete-side `_delete_cookie`; both write and renew
        share it.
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
