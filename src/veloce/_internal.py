"""Semi-public internal utilities — shared across subpackages, not public API.

``.claude/rules/development-guardrails.md`` forbids importing an
underscore-prefixed symbol across a subpackage boundary; this module is the
documented carve-out, importable from any subpackage. Nothing here is in
``veloce/__init__.py``'s ``__all__``, and all of it may change in any release.

A helper earns a place here by being needed **outside the subpackage that would
otherwise own it** (``_ws_handshake_rejection`` is written for ``serving/`` but
is HTTP framing), and by being too small to promote to the public API. Anything
used only by another resident is a private detail of that helper, not a resident
in its own right.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import builtins
import contextlib
import contextvars
import functools
import hashlib
import inspect
import mimetypes
import os
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:  # pragma: no cover
    from veloce.routing.router import RouteInfo, Router

import veloce._json as _json
from veloce._constants import (
    HEADER_CONNECTION,
    HEADER_VALUE_CLOSE,
    HEADER_VALUE_KEEP_ALIVE,
    MIME_JSON,
    MIME_OCTET_STREAM,
    MIME_TEXT_HTML_UTF8,
    MIME_TEXT_PLAIN_UTF8,
    MSG_LABEL_HEADER_NAME,
    MSG_LABEL_SET_COOKIE_VALUE,
)
from veloce._protocol_constants import AUTH_SCHEME_BEARER, SET_COOKIE_JOINER
from veloce.encoders import orjson_default
from veloce.secret import Secret

# Lower-cased once: the head builder tests it against a set of folded names.
HEADER_CONNECTION_LC = HEADER_CONNECTION.lower()

MIME_HTML = MIME_TEXT_HTML_UTF8
MIME_PLAIN = MIME_TEXT_PLAIN_UTF8
MIME_OCTET = MIME_OCTET_STREAM


# Pinned, not from the host registry: RFC 9239 obsoleted one, Windows hides others.
_WEB_MEDIA_TYPES: dict[str, str] = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".css": "text/css",
    ".html": "text/html",
    ".htm": "text/html",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
}

#: The function a decorator is handed and returns unchanged, so `@app.get("/x")`
#: keeps the handler's signature; a bare `Callable` makes Pylance drop it.
T_route = TypeVar("T_route", bound=Callable[..., Any])


@functools.lru_cache(maxsize=512)

# ── Media types ───────────────────────────────────────────
def guess_content_type(path: str) -> str:
    """Return the media type a path's extension names, or the octet-stream default.

    Memoized, because `mimetypes.guess_type` walks the registered table on every
    call and both callers hit the same handful of extensions. Bounded, so a client
    probing arbitrary paths cannot grow it without limit. The cache does not
    observe a `mimetypes.add_type` made after an extension was first guessed.

    `_WEB_MEDIA_TYPES` is consulted first, because `mimetypes` reads the platform
    registry and serving a script as the wrong type is a real failure.
    """
    extension = os.path.splitext(path)[1].lower()
    fixed = _WEB_MEDIA_TYPES.get(extension)
    if fixed is not None:
        return fixed
    return mimetypes.guess_type(path)[0] or MIME_OCTET_STREAM


def _require_slots(cls: type[Any]) -> None:
    """Raise unless `cls` declares its own `__slots__`.

    A slotted base whose subclass forgets `__slots__` silently regains a
    per-instance `__dict__`, undoing the base's memory saving - silently, which
    is why the guardrails call for a structural check. Called from each slotted
    base's `__init_subclass__`.
    """
    if "__slots__" not in cls.__dict__:
        raise TypeError(f"{cls.__name__} must declare __slots__ (use () if it adds no fields)")


# A builtin only from 3.11 (PEP 654); callers re-raise the first on 3.10.
_BaseExceptionGroup: type[BaseException] | None = getattr(builtins, "BaseExceptionGroup", None)

# Built once: `HTTPStatus(code).phrase` walks the IntEnum on every access.
_STATUS_PHRASES: dict[int, str] = {s.value: s.phrase for s in HTTPStatus}


def is_json_mimetype(mimetype: str) -> bool:
    """True for `application/json` or any `application/*+json` subtype.

    Per RFC 6839 Sec. 3.1 the structured suffix `+json` marks the body as
    JSON-encoded. A bare `endswith("json")` over-matches unrelated types such as
    `text/json`, so the suffix test is anchored to the `application/` tree.
    """
    if mimetype == MIME_JSON:
        return True
    return mimetype.startswith("application/") and mimetype.endswith("+json")


def json_body_refused(mimetype: str) -> bool:
    """Whether the declared content type forbids reading the body as JSON.

    Media-type enforcement first: a caller that asked for JSON and declared
    something else is told so (415) rather than parsed on a guess. An absent
    header is a refusal too - absence declares nothing.

    It narrows one CSRF avenue incidentally. `text/plain`, `multipart/form-data`,
    `application/x-www-form-urlencoded` and an absent header are what a
    cross-origin form or `fetch` may send with no preflight, so refusing them
    keeps a JSON endpoint off that path. It does nothing about a same-origin
    attacker, a CORS-permissive app, or one declaring `application/json` from an
    allowed origin: `CSRFMiddleware` and `SameSite` cookies are the control.
    """
    return not is_json_mimetype(mimetype)


# ── Async offload ─────────────────────────────────────────
def offload(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> asyncio.Future[Any]:
    """Run a sync callable in the default executor, preserving request context.

    Returns the awaitable rather than awaiting it, so it adds no extra coroutine
    frame on the hot dispatch path. The callable runs through a `copy_context()`
    snapshot, so request-scoped ContextVars stay bound in the worker thread; the
    snapshot is read-only, so a `set(...)` inside does not propagate back.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return loop.run_in_executor(None, ctx.run, functools.partial(fn, *args, **kwargs))


# ── Value coercion ────────────────────────────────────────
def _coerce_secret_bytes(value: str | bytes | Secret) -> bytes:
    """Unwrap a `Secret` and UTF-8 encode a `str`, returning raw bytes.

    Callers that require a non-empty secret check the returned bytes, which is
    equivalent to checking the pre-encode value since `not b""` and `not ""` are
    both true.
    """
    if isinstance(value, Secret):
        value = value.reveal()
    if isinstance(value, str):
        value = value.encode("utf-8")
    return value


def _refuse_string_scopes(scopes: Any, where: str) -> None:
    """Raise for a bare string where a sequence of scopes is meant.

    Iterating a string yields characters, so `scopes="admin"` silently becomes
    five one-character scopes. Refused on the line that made the mistake rather
    than normalised, which would hide the typo.
    """
    raise TypeError(
        f"{where} takes a sequence of scopes, not {type(scopes).__name__}; "
        f"pass [{scopes!r}] for a single scope."
    )


def _coerce_bool(value: Any) -> bool:
    """Interpret a config flag as a bool, including dotenv-style strings.

    `from_env_file` stores values as plain strings, so `DEBUG=false` arrives as
    the string `"false"` - which `bool(...)` would treat as truthy. Strings are
    matched case-insensitively against the common truthy tokens; everything else
    (real bools, ints, `None`) falls back to `bool`.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _unpack_response_tuple(value: tuple[Any, ...]) -> tuple[Any, int | None, Any] | None:
    """Split a `(body, status)` / `(body, headers)` / `(body, status, headers)` return.

    Returns `(body, status, headers)` with `None` for whatever the tuple did not
    carry, or `None` when `value` is not a response tuple at all - any length but
    two or three, which every caller then treats as a plain value.
    """
    length = len(value)
    if length == 3:
        return value[0], value[1], value[2]
    if length == 2:
        body, second = value
        # `Mapping`, since `Headers` is no `dict`; `str`/`bytes` name a status.
        if isinstance(second, Mapping) or (
            isinstance(second, Iterable) and not isinstance(second, (str, bytes))
        ):
            return body, None, second
        if isinstance(second, int):
            return body, second, None
        return body, int(second), None
    return None


# ── Header encoding and the CRLF guard ────────────────────
def _header_value_has_crlf(value: str) -> bool:
    """Return True if `value` carries CR, LF, or NUL (unsafe in a header)."""
    return "\r" in value or "\n" in value or "\x00" in value


#: Appended on rejection, as a constant so the message is built only on a raise.
_LABEL_HEADER_VALUE_SUFFIX = " header value"


def _reject_header_crlf(value: str, what: str, suffix: str = "") -> str:
    """Reject CR, LF, or NUL in a header field name or value.

    Untrusted data carrying these characters enables HTTP response
    splitting / header injection. Raising - rather than silently
    stripping - surfaces the bug at the offending call site.

    `what` and `suffix` are joined only when raising, so a caller naming the
    field it checks does not build that string per header per response.
    """
    if _header_value_has_crlf(value):
        raise ValueError(f"{what}{suffix} contains an illegal control character (CR, LF, or NUL)")
    return value


# A control character in a request-derived value splits a plain-text log line
# (CWE-117): a percent-decoded `%0a` in the target arrives as a real newline.
_LOG_SANITIZE = str.maketrans({**{c: f"\\x{c:02x}" for c in range(0x20)}, 0x7F: "\\x7f"})


def _encode_header_value(value: str) -> str:
    """Return a latin-1-encodable form of a header value.

    HTTP header values are emitted as latin-1 (the HTTP/1.1 wire encoding
    and the ASGI header contract). The common case is ASCII, which is
    latin-1-safe and returned as-is on the fast path. Values with non-ASCII
    but latin-1-representable characters pass through unchanged. Anything
    outside latin-1 is RFC 2047 MIME-encoded to an ASCII `=?utf-8?b?...?=`
    token rather than raising (HTTP/1.1) or emitting raw UTF-8 (ASGI).

    The caller must have already cleared `_reject_header_crlf`. The token is
    built here rather than through `email.header.Header`, whose `encode()` folds
    on any character Python counts as a line break - so a value carrying U+2028
    comes back as two words joined by a bare LF. One base64 encoded-word has
    nowhere to fold.
    """
    if value.isascii():
        return value
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        token = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"=?utf-8?b?{token}?="
    return value


def _encode_response_head(
    status_code: int,
    default_headers: dict[str, str],
    headers: dict[str, str],
    *,
    keep_alive: bool,
) -> list[str]:
    """Build the HTTP/1.1 status line plus header lines for a response.

    Shared by every raw-transport ``encode()`` so the heads stay in lock-step.

    ``keep_alive`` is required rather than defaulted, because the protocol takes
    the decision: a head hardcoding ``Connection: keep-alive`` would answer an
    HTTP/1.0 request with a closed socket and a header saying otherwise.

    ``default_headers`` are emitted in order, each only when the caller supplied
    no header of the same name (case-insensitively). User ``headers`` follow,
    splitting any joined ``Set-Cookie`` blob. Both are CRLF/NUL-validated, since
    a default value such as ``content_type`` is caller-settable.

    Returns the line list, each ending in CRLF; the caller joins, encodes latin-1
    and appends the blank-line terminator and any body.
    """
    reason = _STATUS_PHRASES.get(status_code, "")
    parts = [f"HTTP/1.1 {status_code} {reason}".rstrip() + "\r\n"]

    # Lowered once, reused below: twice showed up per header per response.
    lowered = [(key, str(key).lower(), value) for key, value in headers.items()]
    user_keys_lc = {key_lower for _key, key_lower, _value in lowered}
    # The transport decides: a hardcoded `keep-alive` may describe a dead socket.
    if HEADER_CONNECTION_LC not in user_keys_lc:
        parts.append(
            f"{HEADER_CONNECTION}: "
            f"{HEADER_VALUE_KEEP_ALIVE if keep_alive else HEADER_VALUE_CLOSE}\r\n"
        )
    for name, value in default_headers.items():
        if name.lower() not in user_keys_lc:
            _reject_header_crlf(value, name, _LABEL_HEADER_VALUE_SUFFIX)
            parts.append(f"{name}: {_encode_header_value(value)}\r\n")

    # Field names are case-insensitive (RFC 9110 Sec. 5.1), and browsers narrow a
    # duplicated `Content-Security-Policy`. `Set-Cookie` is legitimately repeated.
    slot_by_name: dict[str, int] = {}
    for key, key_lower, value in lowered:
        if key_lower == "set-cookie":
            # One entry may join several cookies; validate each line.
            for line in str(value).split(SET_COOKIE_JOINER):
                _reject_header_crlf(line, MSG_LABEL_SET_COOKIE_VALUE)
                parts.append(f"Set-Cookie: {_encode_header_value(line)}\r\n")
        else:
            _reject_header_crlf(str(key), MSG_LABEL_HEADER_NAME)
            sval = str(value)
            _reject_header_crlf(sval, str(key), _LABEL_HEADER_VALUE_SUFFIX)
            line = f"{key}: {_encode_header_value(sval)}\r\n"
            slot = slot_by_name.get(key_lower)
            if slot is None:
                slot_by_name[key_lower] = len(parts)
                parts.append(line)
            else:
                parts[slot] = line
    return parts


async def _write_chunked(
    transport: Any,
    stream: AsyncIterator[bytes],
    drain: Callable[[], Awaitable[None]] | None,
) -> None:
    """Write `stream` to `transport` in HTTP/1.1 chunked framing.

    The write-side twin of `_encode_response_head`, used by every streaming
    response that frames its own body on the raw transport.

    `transport.writelines`, where supported, keeps the size-line, payload and
    trailer as separate buffers rather than concatenating per chunk; a transport
    offering only basic `WriteTransport` falls back to one concatenated write.

    A zero-length chunk is skipped: its framing is the last-chunk terminator
    (RFC 9112 Sec. 7.1), so emitting it mid-stream truncates the body. `drain`,
    when supplied, is awaited after each chunk, so a producer outrunning a slow
    client is throttled instead of growing the write buffer without bound.
    """
    writelines = getattr(transport, "writelines", None)
    async for chunk in stream:
        if not chunk:
            continue
        size = format(len(chunk), "x").encode("ascii")
        if writelines is not None:
            writelines((size, b"\r\n", chunk, b"\r\n"))
        else:
            transport.write(size + b"\r\n" + chunk + b"\r\n")
        if drain is not None:
            await drain()
    transport.write(b"0\r\n\r\n")


# ── ETags ─────────────────────────────────────────────────
def _file_etag(path: str, size: int, mtime: float) -> str:
    """Weak, opaque-quoted ETag derived from (path, size, mtime).

    RFC 9110 Sec. 8.8.3 requires the weak (`W/`) marker when a tag does not
    guarantee byte-for-byte identity, and an mtime-derived tag can collide
    within the same second. Weak compare (Sec. 8.8.3.2) still serves
    `If-None-Match`; `If-Match` and `If-Range` (Sec. 13.1.5, compared strongly)
    refuse these tags, so a range resume uses `Last-Modified` instead.
    """
    key = f"{path}:{size}:{mtime}".encode()
    # `usedforsecurity=False`: an opaque tag, so it must not raise on FIPS builds.
    return f'W/"{hashlib.md5(key, usedforsecurity=False).hexdigest()}"'


def _etag_matches_weak(server_etag: str, client_token: str) -> bool:
    """Weak comparison of two ETag tokens per RFC 9110 Sec. 8.8.3.2.

    Strips surrounding whitespace, an optional `W/` weak marker, and
    the surrounding double quotes on both sides before comparing the
    opaque tags. Returns True when the opaque-tags are equal regardless
    of weak/strong marking - required for `If-None-Match` and `If-Range`
    against weak server validators.
    """
    a = server_etag.strip().removeprefix("W/").strip('"')
    b = client_token.strip().removeprefix("W/").strip('"')
    return a == b


def _preconditions_say_unchanged(
    if_none_match: tuple[str, ...] | list[str],
    if_modified_since: float | None,
    etag: str,
    mtime: float | None,
) -> bool:
    """Whether the read-side preconditions say the client's copy is current.

    RFC 9110 Sec. 13.2 precedence: `If-None-Match` supersedes
    `If-Modified-Since` when both are present, and an explicit non-match ends
    the decision rather than falling through to the date. `*` matches whenever a
    representation exists, wherever it appears in the list (Sec. 13.1.2), and
    the ETag comparison is weak (Sec. 8.8.3.2).
    """
    if if_none_match:
        if "*" in if_none_match:
            return True
        return any(_etag_matches_weak(etag, token) for token in if_none_match)
    # HTTP-dates have second resolution, so mtime 1.5 is not newer than 1.
    return (
        if_modified_since is not None and mtime is not None and int(mtime) <= int(if_modified_since)
    )


def _etag_matches_strong(server_etag: str, client_token: str) -> bool:
    """Strong comparison of two ETag tokens per RFC 9110 Sec. 8.8.3.1.

    Required for `If-Match` (Sec. 13.1.1): two validators match only when
    both are strong - neither carries the `W/` weak marker - and their
    opaque quoted forms are byte-identical. A weak validator on either
    side never satisfies a strong comparison, so a weak server ETag can
    never match an `If-Match` precondition.
    """
    a = server_etag.strip()
    b = client_token.strip()
    if a.startswith("W/") or b.startswith("W/"):
        return False
    return a == b


# ── Base64 ────────────────────────────────────────────────
def _b64encode(data: bytes) -> str:
    """URL-safe base64 with `=` padding stripped."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    """Inverse of `_b64encode`. Re-adds padding before decoding."""
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


# Keyed by callable, weakly so collecting a hook evicts its entry.
_iscoro_cache: weakref.WeakKeyDictionary[Callable[..., Any], bool] = weakref.WeakKeyDictionary()


# ── Callable introspection ────────────────────────────────
def _is_async_callable(fn: Callable[..., Any]) -> bool:
    """Memoised `inspect.iscoroutinefunction` for hot-path hook dispatch.

    Also detects class instances whose `__call__` is `async def` -
    plain `iscoroutinefunction(instance)` returns False for those.
    """
    try:
        cached = _iscoro_cache.get(fn)
    except TypeError:
        return _check_async(fn)
    if cached is not None:
        return cached
    result = _check_async(fn)
    with contextlib.suppress(TypeError):
        _iscoro_cache[fn] = result
    return result


def _check_async(fn: Callable[..., Any]) -> bool:
    if inspect.iscoroutinefunction(fn):
        return True
    # The bound `__call__`: `callable()` says nothing about `async def`.
    call = getattr(fn, "__call__", None)  # noqa: B004
    return call is not None and inspect.iscoroutinefunction(call)


# Default port per scheme; RFC 6454 Sec. 4 treats `https://host` and
# `https://host:443` as one origin. `URL.netloc` strips only an http/https one.
_NETLOC_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443, "ws": 80, "wss": 443}


# ── URL parts ─────────────────────────────────────────────
def is_default_port(scheme: str, port: int | None) -> bool:
    """True when `port` is the default http/https port for `scheme` (or unset)."""
    return port is None or _NETLOC_DEFAULT_PORTS.get(scheme) == port


def strip_default_port(scheme: str, netloc: str) -> str:
    """Drop the scheme's default `:port` suffix from a `host[:port]` netloc.

    Browsers omit `:443`/`:80` from `Origin`/`Referer` for default-port
    requests, so `host` and `host:default` denote the same origin
    (RFC 6454 Sec. 4). A non-default or absent port is left untouched.
    """
    default = DEFAULT_PORTS.get(scheme)
    if default is not None:
        suffix = f":{default}"
        if netloc.endswith(suffix):
            return netloc[: -len(suffix)]
    return netloc


def _extract_host(raw: str, lower: bool = True) -> str:
    """Strip the port from a Host header value, handling IPv6 brackets.

    Bracketed IPv6 (`[2001:db8::1]:8080` / `[2001:db8::1]`), bare IPv6
    (`2001:db8::1`, two or more colons and no brackets), and the IPv4 /
    hostname `host:port` form are each handled so a colon inside an IPv6
    literal is never mistaken for a port separator. `lower` lower-cases the
    result for case-insensitive host comparison (the default); ProxyFix
    passes `lower=False` because it stores the client IP verbatim.
    """
    if "[" in raw:
        host = raw.split("]", 1)[0][1:]
    elif raw.count(":") >= 2:
        host = raw
    else:
        host = raw.split(":", 1)[0]
    return host.lower() if lower else host


def _ws_handshake_rejection(middlewares: Iterable[object], host: str, origin: str) -> bool:
    """Report whether a WebSocket handshake is refused by the host/Origin allow-lists.

    A `websocket` scope never reaches an HTTP middleware's `process_request`, so
    the gate consults the same public predicates the middleware would apply:
    `is_host_allowed(host)` and `is_websocket_origin_allowed(origin)`. Both
    transports share this decision; each maps the refusal onto its own wire form
    (HTTP 403 native, a 1008 close on ASGI).

    The scan walks the live middleware list once per handshake - a cold path over
    a short list, not a per-request or per-frame one.
    """
    for mw in middlewares:
        host_check = getattr(mw, "is_host_allowed", None)
        if host_check is not None and not host_check(host):
            return True
        origin_check = getattr(mw, "is_websocket_origin_allowed", None)
        if origin_check is not None and not origin_check(origin):
            return True
    return False


# The current task's app and request; `helpers` re-exports them as proxies.
_current_app_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "veloce_current_app", default=None
)
_current_request_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "veloce_current_request", default=None
)
# True inside an exception handler, where the send helpers skip range
# negotiation: RFC 9110 Sec. 14.2 scopes a `Range` to the target's own body.
_serving_error_page_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "veloce_serving_error_page", default=False
)

# "Not resolved yet", as against a resolved `None` meaning the direct path.
_UNRESOLVED_JSON_DUMPS: Any = object()


def dumps_for(app: Any, payload: Any) -> bytes:
    """Serialise `payload` through `app`'s provider, or directly without one.

    The one place a JSON payload bound for a client is encoded, so an
    application's dialect cannot reach some surfaces and miss others. `app` is
    `None` outside a request, where the direct encoder applies.

    Here rather than in `json_provider` because `http.response` is the lower
    layer and must be able to call it; `json_provider` re-exports it.
    """
    if app is None:
        return _json.dumps(payload, default=orjson_default)
    # `None` means the stock provider, which emits what the direct call does.
    dumps = app._handler_json_dumps
    if dumps is _UNRESOLVED_JSON_DUMPS:
        dumps = app._handler_json_dumps = app._resolve_handler_json_dumps()
    if dumps is None:
        return _json.dumps(payload, default=orjson_default)
    encoded: bytes = dumps(payload)
    return encoded


def dumps_current(payload: Any) -> bytes:
    """Serialise through the app handling this request, or directly outside one."""
    return dumps_for(_current_app_var.get(), payload)


def _require_methods(cls: type[Any], base: type[Any], names: tuple[str, ...]) -> None:
    """Refuse `cls` unless it supplies a real implementation of every name.

    Abstractness here is a `NotImplementedError` body rather than `abc.ABC`, so
    "supplied" means the attribute resolves to something other than the one
    `base` defines, and a subclass of a *concrete* backend passes on inherited
    implementations.

    Raised at subclass definition, so a forgotten method is an import-time
    `TypeError` naming it rather than a `NotImplementedError` on a live request.

    An intermediate class that deliberately leaves a method to its own
    subclasses marks it `@abc.abstractmethod`, which is exempt.
    """
    missing = [
        name
        for name in names
        if getattr(cls, name, None) is getattr(base, name, None)
        and name not in getattr(cls, "__abstractmethods__", ())
    ]
    if missing:
        raise TypeError(
            f"{cls.__name__} does not implement {base.__name__}: {', '.join(missing)} missing. "
            "For an intermediate class that concrete subclasses complete, mark the method "
            "`@abc.abstractmethod` - `__abstractmethods__` is exempt from this check."
        )


def _quote_header_value(value: str) -> str:
    """Escape a string for the inside of an HTTP quoted-string.

    RFC 9110 Sec. 5.6.4: a backslash begins a quoted-pair, so both the
    backslash and the double-quote must be escaped - and the backslash first,
    or the backslash written to escape a quote would itself be doubled.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _close_form_uploads(form: Any) -> None:
    """Close the spool files a multipart parse opened.

    Takes the form rather than the request, so a caller that only needs the
    files does not retain the whole `Request` to reach them.
    """
    if form is None:
        return
    for value in form.values():
        handle = getattr(value, "file", None)
        if handle is not None and not getattr(handle, "closed", True):
            with contextlib.suppress(Exception):
                handle.close()


def _decode_basic_credentials(payload: str) -> tuple[str, str] | None:
    """Decode an RFC 7617 `Basic` payload into `(userid, password)`, or `None`.

    `None` means malformed: base64 that does not decode as UTF-8, or a decoded
    value with no colon. RFC 7617 Sec. 2 makes the colon mandatory -
    `userid ":" password` - so a colon-less payload is not credentials at all.

    Surrounding whitespace is trimmed, because RFC 9110 Sec. 11.6.1 allows more
    than one SP between the auth-scheme and the token68 and field values carry
    optional whitespace, so it is not part of the credential.
    """
    stripped = payload.strip()
    # RFC 4648 Sec. 4 pads to four; `validate=True` only catches it on 3.12+.
    if len(stripped) % 4:
        return None
    try:
        decoded = base64.b64decode(stripped, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    userid, separator, password = decoded.partition(":")
    if not separator:
        return None
    return userid, password


_BEARER_PREFIX = AUTH_SCHEME_BEARER + " "
_BEARER_PREFIX_LOWER = _BEARER_PREFIX.lower()
_BEARER_PREFIX_LEN = len(_BEARER_PREFIX)


def _bearer_token_from(auth: str, scheme: str = AUTH_SCHEME_BEARER) -> str | None:
    """Return the bearer token in an `Authorization` value, or `None`.

    Pure extraction: no exception, no challenge. `security/_utils` wraps it with
    the `auto_error` behaviour a security scheme needs; the MCP HTTP transport,
    which owes the caller a challenge response rather than a raise, calls it here.

    RFC 6750 Sec. 2.1 + RFC 7235: only SP/HTAB are permitted between the scheme
    and the token, so other Unicode whitespace (NBSP, newline) is not trimmed.
    """
    if scheme == AUTH_SCHEME_BEARER:
        prefix_len = _BEARER_PREFIX_LEN
        prefix_lower = _BEARER_PREFIX_LOWER
    else:
        prefix = scheme + " "
        prefix_len = len(prefix)
        prefix_lower = prefix.lower()
    if auth[:prefix_len].lower() != prefix_lower:
        return None
    return auth[prefix_len:].strip(" 	") or None


# One home: `routing.converters` and `contrib.openapi` share the one document.
_SCALAR_JSON_SCHEMAS: dict[str, dict[str, Any]] = {
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "uuid": {"type": "string", "format": "uuid"},
    "date": {"type": "string", "format": "date"},
    "date-time": {"type": "string", "format": "date-time"},
    "time": {"type": "string", "format": "time"},
    # `path` is unregistered, but `format` is open, so it still tells a client.
    "path": {"type": "string", "format": "path"},
}


def _readd_route(
    target: Router, full_path: str, methods: list[str], info: RouteInfo, endpoint: str
) -> None:
    """Re-register `info` onto `target` under `full_path`/`endpoint`.

    The `RouteInfo`-to-`add_route` field mapping, in one place so a new
    `RouteInfo` field is forwarded on every re-registration path. Shared by
    blueprint splicing and nested blueprint composition, both of which sit
    outside `routing/`.
    """
    target.add_route(
        path=full_path,
        handler=info.handler,
        methods=methods,
        strict_slashes=info.strict_slashes,
        dependencies=info.dependencies,
        response_model=info.response_model,
        tags=info.tags,
        summary=info.summary,
        name=endpoint,
        description=info.description,
        deprecated=info.deprecated,
        response_description=info.response_description,
        status_code=info.status_code,
        response_class=info.response_class,
        response_model_include=info.response_model_include,
        response_model_exclude=info.response_model_exclude,
        response_model_exclude_unset=info.response_model_exclude_unset,
        response_model_exclude_defaults=info.response_model_exclude_defaults,
        response_model_by_alias=info.response_model_by_alias,
        response_model_exclude_none=info.response_model_exclude_none,
        include_in_schema=info.include_in_schema,
        responses=info.responses,
        operation_id=info.operation_id,
        openapi_extra=info.openapi_extra,
        defaults=info.defaults,
        callbacks=info.callbacks,
        subdomain=info.subdomain,
        host=info.host,
        expose_as_mcp_tool=info.expose_as_mcp_tool,
        mcp_description=info.mcp_description,
        expose_as_mcp_resource=info.expose_as_mcp_resource,
        mcp_resource_uri=info.mcp_resource_uri,
        mcp_resource_mime_type=info.mcp_resource_mime_type,
        mcp_resource_size=info.mcp_resource_size,
        mcp_resource_annotations=info.mcp_resource_annotations,
        mcp_meta=info.mcp_meta,
        stream=info.stream,
        mcp_scopes=list(info.mcp_scopes) if info.mcp_scopes else None,
        mcp_icons=info.mcp_icons,
        mcp_task_support=info.mcp_task_support,
        exclude_middleware=(
            [*info.excluded_middleware[0], *info.excluded_middleware[1]]
            if info.excluded_middleware
            else None
        ),
    )


def resolve_hint_target(obj: Any) -> Any:
    """Return the object whose annotations describe `obj`'s call signature.

    `inspect.signature` transparently follows `functools.partial` to its `func`,
    a class to `__init__` and a callable instance to `type(obj).__call__`;
    `get_type_hints` follows none of them and would read the class body instead.
    """
    real = obj
    while isinstance(real, functools.partial):
        real = real.func
    if inspect.isclass(real):
        return real.__init__
    if not inspect.isfunction(real) and not inspect.ismethod(real) and callable(real):
        return type(real).__call__
    return real
