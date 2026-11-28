"""Cookie-session middleware — the whole session in a signed, timestamped cookie."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from veloce.audit import Finding
from veloce.http.cookies import dump_cookie
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.sessions import Session
from veloce.signing import BadSignature

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    from veloce.audit import AuditContext

from veloce.middleware.sessions._shared import (
    _CHUNK_SEP,
    _DEFAULT_MAX_COOKIE_CHUNKS,
    _DEFAULT_MAX_COOKIE_SIZE,
    _UNSET,
    _begin_session_response,
    _build_signer,
    _reassemble_chunks,
    _should_persist,
    _UnsetType,
)
from veloce.middleware.sessions.base import (
    SessionMiddlewareBase,
)

# A literal, not `__name__`: "veloce.sessions" is what callers filter on.
_logger = logging.getLogger("veloce.sessions")


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
        *,
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
        """Configure the signed-cookie session.

        Args:
            secret_key: Signing key, or keys newest-first for rotation. When omitted it is
                read from `app.secret_key` on the first request.
            cookie_name: Cookie name; the library default when omitted.
            max_age: Seconds a non-permanent session lasts.
            path: Cookie `Path`; the library default when omitted.
            httponly: Cookie `HttpOnly`; the library default when omitted.
            secure: Cookie `Secure`; the library default when omitted.
            samesite: Cookie `SameSite`; the library default when omitted.
            domain: Cookie `Domain`.
            permanent_lifetime: Seconds a `session.permanent` session lasts.
            max_cookie_size: Largest cookie, in bytes, the session may serialise to.
            vary_on_cookie: Add `Vary: Cookie` to responses that read the session.
            persist_on_status: Decides from the status code whether to save the session;
                by default any status below 500.
            cookie_prefix: Apply the `__Host-` or `__Secure-` prefix to the cookie name.
            partitioned: Mark the cookie `Partitioned`.
            renew_on_access: Refresh the lifetime on every request that reads the session.
            chunked: Split a large session across several cookies.
            max_chunks: Most cookies a chunked session may use.
            name: Name for `exclude_middleware`.

        Raises:
            ValueError: If `max_chunks` is below 1, or the cookie settings are an
                invalid combination (a prefix without `Secure`, `__Host-` without
                `path="/"` or with a `domain`, `partitioned` without `Secure` and
                `SameSite=None`).

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

        Args:
            config: The application config holding `SECRET_KEY`.
            hint: Appended to the error message to say when the key was needed.

        Raises:
            RuntimeError: If the constructor was given no key and `config` has none.

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
