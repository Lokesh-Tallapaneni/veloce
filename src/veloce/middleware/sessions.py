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
import warnings
from collections.abc import Callable
from typing import Any, Literal

from veloce._constants import HEADER_COOKIE
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


class SessionMiddleware(Middleware):
    """Server-side session stored in a signed, timestamped cookie.

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
        secret_key: str | list[str],
        cookie_name: str = "session",
        max_age: int = 86400 * 14,
        path: str = "/",
        httponly: bool = True,
        secure: bool = False,
        samesite: str = "lax",
        domain: str | None = None,
        permanent_lifetime: int = 86400 * 31,
        max_cookie_size: int = _DEFAULT_MAX_COOKIE_SIZE,
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
        keys = [secret_key] if isinstance(secret_key, str) else list(secret_key)
        if not keys:
            raise ValueError("secret_key must be a non-empty string or list of strings")
        _validate_cookie_security(
            cookie_prefix=cookie_prefix,
            partitioned=partitioned,
            domain=domain,
            path=path,
            secure=secure,
            samesite=samesite,
        )
        self._signer = Signer(keys[0], salt="veloce.session")
        for fallback in keys[1:]:
            self._signer.add_fallback_secret(fallback, salt="veloce.session")
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.path = path
        self.httponly = httponly
        self.secure = secure
        self.samesite = samesite
        self.domain = domain
        self.cookie_prefix = cookie_prefix
        self.partitioned = partitioned
        self._samesite_cap = self.samesite.capitalize() if self.samesite else None
        # The wire name carries the RFC 6265bis prefix; the request side must
        # read under the same name. `__init__`-time derivation keeps the
        # per-request read off the hot path.
        if cookie_prefix == "host":
            self._wire_cookie_name = f"__Host-{cookie_name}"
        elif cookie_prefix == "secure":
            self._wire_cookie_name = f"__Secure-{cookie_name}"
        else:
            self._wire_cookie_name = cookie_name
        # `PERMANENT_SESSION_LIFETIME` analog - used for the cookie
        # `Max-Age` when `session.permanent` is set. Defaults to 31 days.
        self.permanent_lifetime = permanent_lifetime
        self.max_cookie_size = max_cookie_size
        # Emit `Vary: Cookie` on responses that write the session cookie, so a
        # shared cache keyed on URL alone can't serve one user's session-bearing
        # body to another (RFC 9110 Sec. 12.5.5). Set False to opt out.
        self.vary_on_cookie = vary_on_cookie
        # Policy deciding whether to persist for a given response status. The
        # `None` default means "persist for status < 500" - a failed request
        # should not write a half-mutated session (Set-Cookie / store write).
        self._persist_on_status = persist_on_status
        # Sliding expiry: when True, a session merely *read* during the request
        # (accessed, not modified) gets its cookie re-signed on the way out, so
        # its server-enforced timestamp and `Max-Age` roll forward and an active
        # user is never logged out mid-session. Default False keeps the prior
        # behavior (only a modified session is re-written).
        self.renew_on_access = renew_on_access
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

    async def process_request(self, request: Request) -> Response | None:
        """Load the session from the signed cookie into request state."""
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
        # No session attached (handler bypassed middleware?) -> nothing to do.
        if session is None:
            return response

        # Read `.accessed` / `.modified` once and reuse for the `Vary` and the
        # persist decisions. `getattr` with a default tolerates a non-`Session`
        # object placed under the reserved `session` state key (the key is
        # framework-owned, but `request._state` is mutable scratch space), in
        # which case the session work is simply skipped, as before.
        accessed = getattr(session, "accessed", False)
        modified = getattr(session, "modified", False)

        # Emit `Vary: Cookie` when a handler actually accessed the session, so a
        # response personalized from session contents is not shared across users
        # by a URL-keyed cache (RFC 9110 Sec. 12.5.5). A session-independent
        # response (handler never touched `request.session`) stays cacheable.
        if self.vary_on_cookie and (accessed or modified):
            response.add_vary(HEADER_COOKIE)

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
        if not self._should_persist(response.status_code):
            return response

        if not session:
            self._delete_cookie(response, self.cookie_name, prefix=True)
            # Clear any chunk cookies a previous larger session left behind, so
            # an emptied session does not resurrect from stale `<name>.N` parts.
            if self.chunked:
                self._clear_chunks(response)
            return response

        cookie_value = self._signer.dumps(session)
        # A `permanent` session uses the longer lifetime for `Max-Age`.
        lifetime = self.permanent_lifetime if getattr(session, "permanent", False) else self.max_age
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
        appended here - the constructor guard guarantees `secure=True` when set,
        so it is always valid. `prefix` is True only for the base cookie passed
        by its bare `cookie_name`; chunk cookies pass their already-prefixed wire
        name with `prefix=False`.
        """
        rendered = dump_cookie(
            name,
            value,
            max_age=lifetime,
            path=self.path,
            domain=self.domain,
            httponly=self.httponly,
            secure=self.secure,
            samesite=self._samesite_cap,
            prefix=self.cookie_prefix if prefix else None,
        )
        if self.partitioned:
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

    def _delete_cookie(self, response: Response, name: str, *, prefix: bool) -> None:
        """Tell the client to drop `name` using this middleware's attribute set.

        `prefix` is True only for the base cookie passed by its bare name; chunk
        cookies pass their already-prefixed wire name with `prefix=False`.
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

    def _should_persist(self, status_code: int) -> bool:
        """Whether the (modified) session should be written for this status."""
        policy = self._persist_on_status
        return policy(status_code) if policy is not None else status_code < 500


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

    Set `renew_on_access=True` for sliding expiry: a session that was only read
    during a request has its store TTL refreshed (via `SessionStore.touch`) and
    its cookie re-stamped on the way out - an idle-timeout reset. Default off.
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
        domain: str | None = None,
        vary_on_cookie: bool = True,
        persist_on_status: Callable[[int], bool] | None = None,
        cookie_prefix: Literal["host", "secure"] | None = None,
        partitioned: bool = False,
        renew_on_access: bool = False,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        _validate_cookie_security(
            cookie_prefix=cookie_prefix,
            partitioned=partitioned,
            domain=domain,
            path=path,
            secure=secure,
            samesite=samesite,
        )
        self.store = store if store is not None else InMemorySessionStore()
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.path = path
        self.httponly = httponly
        self.secure = secure
        self.samesite = samesite
        self.domain = domain
        self.cookie_prefix = cookie_prefix
        self.partitioned = partitioned
        # See SessionMiddleware: emit `Vary: Cookie` on session-cookie writes,
        # and skip persistence on 5xx by default. Same semantics here.
        self.vary_on_cookie = vary_on_cookie
        self._persist_on_status = persist_on_status
        # Sliding expiry: when True, a session merely *read* during the request
        # has its server-side store TTL and cookie `Max-Age` refreshed on the
        # way out (see SessionMiddleware). Default False keeps prior behavior.
        self.renew_on_access = renew_on_access
        # Read/write must share the prefixed wire name (see SessionMiddleware).
        if cookie_prefix == "host":
            self._wire_cookie_name = f"__Host-{cookie_name}"
        elif cookie_prefix == "secure":
            self._wire_cookie_name = f"__Secure-{cookie_name}"
        else:
            self._wire_cookie_name = cookie_name

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
        if session is None:
            return response

        # Read `.accessed` / `.modified` once; `getattr` with a default tolerates
        # a non-`Session` object placed under the reserved `session` state key,
        # skipping the session work as before.
        accessed = getattr(session, "accessed", False)
        modified = getattr(session, "modified", False)

        # See SessionMiddleware: emit `Vary: Cookie` only when a handler accessed
        # the session (read or write), so session-independent responses stay
        # cacheable.
        if self.vary_on_cookie and (accessed or modified):
            response.add_vary(HEADER_COOKIE)

        if not modified:
            # Sliding expiry: an existing session that was only read (accessed,
            # not new) has its store TTL and cookie `Max-Age` refreshed, then
            # we are done - the payload is unchanged so there is no store write.
            if (
                self.renew_on_access
                and accessed
                and not getattr(session, "new", False)
                and self._should_persist(response.status_code)
            ):
                await self._renew(request, response)
            return response

        # Do not persist (store write or cookie change) on a 5xx by default.
        if not self._should_persist(response.status_code):
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
            domain=self.domain,
            httponly=self.httponly,
            secure=self.secure,
            samesite=self.samesite,
            partitioned=self.partitioned,
            prefix=self.cookie_prefix,
        )
        return response

    def _should_persist(self, status_code: int) -> bool:
        """Whether the (modified) session should be written for this status."""
        policy = self._persist_on_status
        return policy(status_code) if policy is not None else status_code < 500

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
        if not await self.store.touch(session_id, self.max_age):
            self._clear_session_cookie(response)
            return
        response.set_cookie(
            self.cookie_name,
            session_id,
            max_age=self.max_age,
            path=self.path,
            domain=self.domain,
            httponly=self.httponly,
            secure=self.secure,
            samesite=self.samesite,
            partitioned=self.partitioned,
            prefix=self.cookie_prefix,
        )

    def _clear_session_cookie(self, response: Response) -> None:
        """Tell the client to drop the session cookie. Single place that
        knows how this middleware's cookie attribute set maps to
        `delete_cookie` - three callers all share the same kwargs."""
        response.delete_cookie(
            self.cookie_name,
            path=self.path,
            domain=self.domain,
            secure=self.secure,
            httponly=self.httponly,
            samesite=self.samesite,
            partitioned=self.partitioned,
            prefix=self.cookie_prefix,
        )
