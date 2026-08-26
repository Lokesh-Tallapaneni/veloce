"""Semi-public internal utilities — shared across subpackages, not part of the public API.

The codebase guardrail in ``.claude/rules/development-guardrails.md`` under
"Cross-Subpackage Imports" forbids importing underscore-prefixed symbols
across subpackage boundaries. This module is the documented carve-out:
symbols defined here (``_reject_header_crlf``, ``_file_etag``, ``_b64encode``,
``_is_async_callable``, ``_extract_host``, ``_ws_handshake_rejection``, and the
MIME / status-phrase constants) ARE permitted to be imported from any subpackage -
``http/``, ``middleware/``, ``security/``, ``serving/``, ``contrib/``,
``routing/`` - because they are explicitly internal-to-the-framework
helpers with a stable contract.

The leading underscore signals "not for users"; this module's existence
and docstring signal "stable for internal use across the framework".
External users must not depend on these symbols - they are not in
``veloce/__init__.py``'s ``__all__`` and may change in any release.

When adding a new helper here, it must be (a) genuinely needed by two
or more subpackages, and (b) small enough that promoting it to the
public API would be premature. Otherwise prefer a public utility or
keep it inside the owning subpackage.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import contextlib
import contextvars
import functools
import hashlib
import inspect
import mimetypes
import os
import sys
import weakref
from collections.abc import Callable, Iterable
from email.header import Header
from http import HTTPStatus
from typing import Any

import orjson

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
from veloce._protocol_constants import SET_COOKIE_JOINER
from veloce.encoders import orjson_default
from veloce.secret import Secret

# Lower-cased once: the head builder tests it against a set of folded names.
HEADER_CONNECTION_LC = HEADER_CONNECTION.lower()

MIME_HTML = MIME_TEXT_HTML_UTF8
MIME_PLAIN = MIME_TEXT_PLAIN_UTF8
MIME_OCTET = MIME_OCTET_STREAM


# Media types pinned to their standard spelling rather than whatever the host
# registry says. RFC 9239 obsoleted `application/javascript` in favour of
# `text/javascript`; the rest are here because a Windows registry entry can
# shadow or omit them entirely.
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


@functools.lru_cache(maxsize=512)
def guess_content_type(path: str) -> str:
    """Return the media type a path's extension names, or the octet-stream default.

    `mimetypes.guess_type` walks the registered table on every call, and both
    callers - the static server and every `FileResponse` - hit the same handful
    of extensions repeatedly, so the answer is memoized. Bounded, so a client
    probing arbitrary paths cannot grow it without limit.

    The cache does not observe a `mimetypes.add_type` made after an extension
    has already been guessed once. Registering media types at import time, as
    the stdlib intends, is unaffected.

    A handful of web types are answered from `_WEB_MEDIA_TYPES` before the
    stdlib is consulted, because `mimetypes` reads the platform registry: on
    Windows `.js` resolves to the obsolete `application/javascript`, and `.json`
    or `.svg` can be absent or remapped by whatever software last claimed the
    extension. Serving a script as the wrong type is a real failure - a strict
    client refuses it - so these do not get to vary by host.
    """
    extension = os.path.splitext(path)[1].lower()
    fixed = _WEB_MEDIA_TYPES.get(extension)
    if fixed is not None:
        return fixed
    return mimetypes.guess_type(path)[0] or MIME_OCTET_STREAM


# `BaseExceptionGroup` is a builtin only from Python 3.11 (PEP 654); on 3.10 the
# name is absent. Resolved once here so the lifespan-unwind, debug, and
# dependency-teardown paths share one platform shim: callers group multiple
# failures on 3.11+ and re-raise the first on 3.10 when grouping is unavailable.
_BaseExceptionGroup: type[BaseException] | None = getattr(builtins, "BaseExceptionGroup", None)

# Reason-phrase lookup - `HTTPStatus(code).phrase` walks the IntEnum on
# every access. Build the mapping once at import time.
_STATUS_PHRASES: dict[int, str] = {s.value: s.phrase for s in HTTPStatus}


def is_json_mimetype(mimetype: str) -> bool:
    """True for `application/json` or any `application/*+json` subtype.

    Single source of the JSON content-type predicate consumed by
    `Request.is_json`, `Response.is_json`, and the MCP tool serialiser.
    Per RFC 6839 Sec. 3.1 the structured-suffix `+json` (e.g.
    `application/vnd.api+json`, `application/problem+json`) marks the body
    as JSON-encoded; a bare `endswith("json")` over-matches unrelated types
    such as `text/json`, so the suffix test is anchored to the
    `application/` tree.
    """
    if mimetype == MIME_JSON:
        return True
    return mimetype.startswith("application/") and mimetype.endswith("+json")


def json_body_refused(mimetype: str) -> bool:
    """Whether a declared content type forbids reading the body as JSON.

    An absent header declares nothing and is not a refusal: plenty of clients
    omit it, and a browser cannot omit it on a cross-origin send. A declared
    non-JSON type is a refusal, which is what closes the CSRF avenue -
    `text/plain`, `multipart/form-data` and `application/x-www-form-urlencoded`
    are the types a cross-origin form or `fetch` may send with no CORS
    preflight, so an endpoint that parses a body under one of them can be
    driven through a cookie-authenticated victim's browser.

    Single source of the rule for every door that reads a request body as
    JSON, so a new one cannot answer differently.
    """
    return bool(mimetype) and not is_json_mimetype(mimetype)


def offload(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> asyncio.Future[Any]:
    """Run a sync callable in the default executor, preserving request context.

    Returns the awaitable (call sites do `await offload(...)`) so it adds no
    extra coroutine frame on the hot dispatch path - only one function-call
    frame, which is negligible against the thread-pool hop itself. The callable
    runs through a `copy_context()` snapshot so request-scoped ContextVars
    (`request`/`session`/`g`/`current_app` proxies, `flash()`) stay bound in the
    worker thread; omitting that wrap makes them read "unbound". The snapshot is
    read-only from the caller's view - a `ContextVar.set(...)` inside the sync
    callable does not propagate back.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return loop.run_in_executor(None, ctx.run, functools.partial(fn, *args, **kwargs))


def _coerce_secret_bytes(value: str | bytes | Secret) -> bytes:
    """Unwrap a `Secret` and UTF-8 encode a `str`, returning raw bytes.

    Single source of the secret/salt coercion shared by `veloce.signing` and
    `veloce.passwords`. Callers that require a non-empty secret check the
    returned bytes (`if not coerced:`), which is equivalent to checking the
    pre-encode `str`/`bytes` since `not b""` and `not ""` are both true.
    """
    if isinstance(value, Secret):
        value = value.reveal()
    if isinstance(value, str):
        value = value.encode("utf-8")
    return value


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


def _coerce_int(value: Any, *, name: str) -> int:
    """Interpret a config value as an int, including dotenv-style strings.

    `from_env_file` stores values as plain strings, so `MAX_COOKIE_SIZE=2048`
    arrives as `"2048"`. A real int passes through; an unparseable value raises a
    clear error naming the config key rather than crashing later on `<` / `max()`.
    """
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{name} must be an integer, got {value!r}") from err


def _header_value_has_crlf(value: str) -> bool:
    """Return True if `value` carries CR, LF, or NUL (unsafe in a header)."""
    return "\r" in value or "\n" in value or "\x00" in value


#: Appended to a field name when a header value is rejected. Kept as a constant
#: so the message is built only on the raise.
_LABEL_HEADER_VALUE_SUFFIX = " header value"


def _reject_header_crlf(value: str, what: str, suffix: str = "") -> str:
    """Reject CR, LF, or NUL in a header field name or value.

    Untrusted data carrying these characters enables HTTP response
    splitting / header injection. Raising - rather than silently
    stripping - surfaces the bug at the offending call site.

    `what` and `suffix` are joined only when raising. A caller naming the field
    it is checking would otherwise build `f"{name} header value"` per header per
    response for a string almost every call discards.
    """
    if _header_value_has_crlf(value):
        raise ValueError(f"{what}{suffix} contains an illegal control character (CR, LF, or NUL)")
    return value


# Control characters (the C0 range plus DEL) in a request-derived value would let
# an attacker forge or split a plain-text log line (CWE-117): a percent-decoded
# `%0a` in the request target arrives as a real newline that a text formatter
# writes as a new physical record. `_LOG_SANITIZE` escapes every control
# character before it reaches a log sink; a normal method / path holds none, so
# `str.translate` over it is a straight pass.
_LOG_SANITIZE = str.maketrans({**{c: f"\\x{c:02x}" for c in range(0x20)}, 0x7F: "\\x7f"})


def _encode_header_value(value: str) -> str:
    """Return a latin-1-encodable form of a header value.

    HTTP header values are emitted as latin-1 (the HTTP/1.1 wire encoding
    and the ASGI header contract). The common case is ASCII, which is
    latin-1-safe and returned as-is on the fast path. Values with non-ASCII
    but latin-1-representable characters pass through unchanged. Anything
    outside latin-1 is RFC 2047 MIME-encoded to an ASCII `=?utf-8?b?...?=`
    token rather than raising (HTTP/1.1) or emitting raw UTF-8 (ASGI).

    The caller must have already cleared `_reject_header_crlf`. `maxlinelen`
    is `sys.maxsize` so `Header.encode()` never folds a long value onto
    multiple CRLF-separated lines - which would re-introduce newlines into
    the header bytes.
    """
    if value.isascii():
        return value
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return Header(value, "utf-8", maxlinelen=sys.maxsize).encode()
    return value


def _encode_response_head(
    status_code: int,
    default_headers: dict[str, str],
    headers: dict[str, str],
    *,
    keep_alive: bool,
) -> list[str]:
    """Build the HTTP/1.1 status line plus header lines for a response.

    Shared by ``Response.encode()``, ``StreamingResponse.encode()`` and
    ``EventSourceResponse.encode()`` so the raw-transport heads stay in
    lock-step.

    ``keep_alive`` says whether the connection survives this response. It is
    required rather than defaulted: each head used to hardcode
    ``Connection: keep-alive`` while the protocol took the actual decision, so
    an HTTP/1.0 request, or one asking for ``Connection: close``, was answered
    by a server that closed the socket and a header saying it had not. Making
    it required means a response type added later cannot inherit a default that
    contradicts its transport. ``default_headers`` are framework defaults applied
    in their given order; each is emitted only when the caller has not
    supplied a header of the same name (case-insensitive), so a
    lower-cased ``content-type`` override does not produce a duplicate.

    User ``headers`` are then emitted in order, splitting any joined
    ``Set-Cookie`` blob and rejecting CR/LF/NUL in names and values.
    Default values are CRLF/NUL-validated too: their names are
    framework constants but a value such as ``content_type`` is a
    public, caller-settable constructor argument, so it must clear the
    same response-splitting check before it reaches the wire.
    Returns the line list (each ending in CRLF); the caller joins,
    encodes latin-1, and appends the blank-line terminator and any body.
    """
    reason = _STATUS_PHRASES.get(status_code, "")
    parts = [f"HTTP/1.1 {status_code} {reason}".rstrip() + "\r\n"]

    # Lowered once per user header and reused below: the duplicate-slot map
    # needs the same value, and computing it twice showed up on a path that runs
    # per header per response.
    lowered = [(key, str(key).lower(), value) for key, value in headers.items()]
    user_keys_lc = {key_lower for _key, key_lower, _value in lowered}
    # The transport decides whether the connection survives; every head used to
    # state `keep-alive` regardless, so a socket the server was about to close
    # was described as reusable.
    if HEADER_CONNECTION_LC not in user_keys_lc:
        parts.append(
            f"{HEADER_CONNECTION}: "
            f"{HEADER_VALUE_KEEP_ALIVE if keep_alive else HEADER_VALUE_CLOSE}\r\n"
        )
    for name, value in default_headers.items():
        if name.lower() not in user_keys_lc:
            _reject_header_crlf(value, name, _LABEL_HEADER_VALUE_SUFFIX)
            parts.append(f"{name}: {_encode_header_value(value)}\r\n")

    # HTTP field names are case-insensitive (RFC 9110 Sec. 5.1), so two
    # spellings of one field must not both reach the wire - a duplicate
    # `Content-Security-Policy` is intersected by browsers to the most
    # restrictive, silently narrowing the policy the later writer intended.
    # The slot map lets a second spelling overwrite the first. `Set-Cookie` is
    # excluded: it is legitimately multi-valued.
    slot_by_name: dict[str, int] = {}
    for key, key_lower, value in lowered:
        if key_lower == "set-cookie":
            # One `Set-Cookie` dict entry may carry several cookies joined
            # by the internal separator; emit and CRLF-validate each line.
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


def _file_etag(path: str, size: int, mtime: float) -> str:
    """Weak, opaque-quoted ETag derived from (path, size, mtime).

    RFC 9110 Sec. 8.8.3 - entity-tags must be marked weak (`W/`) when they
    do not guarantee byte-for-byte identity. mtime-derived tags can
    collide within the same second across content-altering writes, so
    a weak validator is the only spec-compliant choice. Weak compare
    (Sec. 8.8.3.2) still lets `If-None-Match` / `If-Range` work for cache
    revalidation; strict `If-Match` correctly refuses these tags.
    """
    key = f"{path}:{size}:{mtime}".encode()
    # `usedforsecurity=False` so the cache validator does not raise on FIPS
    # builds (the hash is an opaque tag, not a security primitive).
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


def _b64encode(data: bytes) -> str:
    """URL-safe base64 with `=` padding stripped."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    """Inverse of `_b64encode`. Re-adds padding before decoding."""
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


# Memoised `inspect.iscoroutinefunction` results keyed by the callable.
# WeakKeyDictionary so GC of a hook auto-evicts its entry.
_iscoro_cache: weakref.WeakKeyDictionary[Callable[..., Any], bool] = weakref.WeakKeyDictionary()


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
    # Inspect the bound `__call__` directly - `callable()` would tell us
    # whether `fn` is callable, not whether its `__call__` is `async def`.
    call = getattr(fn, "__call__", None)  # noqa: B004
    return call is not None and inspect.iscoroutinefunction(call)


# The default transport port for each URL scheme (RFC 6454 Sec. 4 -
# origin equivalence treats `https://host` and `https://host:443` as the
# same origin). CSRF origin matching canonicalises ws/wss origins too, so
# its strip form spans all four schemes. The URL build form (`URL.netloc`)
# deliberately does NOT: it only suppresses an explicit http/https default
# port and emits a ws/wss `:80`/`:443` verbatim, preserving the absolute-URL
# strings Veloce produced before the strip/build helpers were unified.
_NETLOC_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443, "ws": 80, "wss": 443}


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
    the handshake gate consults the same public predicates the middleware would
    apply: `is_host_allowed(host)` and `is_websocket_origin_allowed(origin)`.
    The two transports (native raw-socket and ASGI) share this single policy
    decision so the predicate set and evaluation order stay in lock-step; each
    caller maps the refusal onto its own wire form (an HTTP 403 on the native
    path, a 1008 close on the ASGI path). Returns `True` when the handshake is
    refused, `False` when it is allowed.

    The scan walks the live middleware list once per handshake. Handshakes are a
    cold path over a short list (typically 1-7 middlewares), so per-connection
    iteration here is intentional - it is not a per-request, per-frame, or
    per-message path.
    """
    for mw in middlewares:
        host_check = getattr(mw, "is_host_allowed", None)
        if host_check is not None and not host_check(host):
            return True
        origin_check = getattr(mw, "is_websocket_origin_allowed", None)
        if origin_check is not None and not origin_check(origin):
            return True
    return False


# The active app and request for the current task. They live here rather than in
# `helpers` because four subpackages (`app`, `contrib`, `middleware`) read them
# directly, and a public module is the wrong place to reach into for a private
# name. `helpers` re-exports them for the `current_app` / `request` proxies.
_current_app_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "veloce_current_app", default=None
)
_current_request_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "veloce_current_request", default=None
)


def dumps_for(app: Any, payload: Any) -> bytes:
    """Serialise `payload` through `app`'s provider, or directly without one.

    The one place a JSON payload bound for a client is encoded. Every surface
    that sends one - a response body, a websocket frame, a server-sent event -
    goes through here, so an application's dialect cannot reach some of them and
    miss others. `app` is `None` outside a request, where there is no provider
    to ask and the direct encoder applies.

    Lives here rather than in `json_provider` because `http.response` is the
    lower layer and must be able to call it: the provider knows about responses,
    not the other way round. `json_provider` re-exports it under its documented
    name.
    """
    if app is None:
        return orjson.dumps(payload, default=orjson_default)
    # The app resolves its provider once and caches `None` when the stock one
    # with nothing configured is active, because that emits exactly what the
    # direct call does. Reading the cache rather than going through
    # `app.json.dumps` unconditionally keeps an application that configured no
    # dialect on the same direct path it was on before.
    dumps = app._handler_json_dumps
    if dumps is _UNRESOLVED_JSON_DUMPS:
        dumps = app._handler_json_dumps = app._resolve_handler_json_dumps()
    if dumps is None:
        return orjson.dumps(payload, default=orjson_default)
    encoded: bytes = dumps(payload)
    return encoded


def dumps_current(payload: Any) -> bytes:
    """Serialise through the app handling this request, or directly outside one."""
    return dumps_for(_current_app_var.get(), payload)


# Distinguishes "the handler-JSON serialiser has not been resolved yet" from a
# resolved `None`, which means "take the direct path". Lives here because the
# app's core resolves it and the dispatch mixin reads it, and those two cannot
# import each other.
_UNRESOLVED_JSON_DUMPS: Any = object()


def _require_methods(cls: type, base: type, names: tuple[str, ...]) -> None:
    """Refuse `cls` unless it supplies a real implementation of every name.

    Abstractness in this codebase is a `NotImplementedError` body rather than
    `abc.ABC`, so "supplied" means the attribute resolves to something other
    than the one `base` defines. A subclass of a *concrete* backend therefore
    passes on inherited implementations, which is how a deployment usually
    specialises one.

    Raised at subclass definition, so a forgotten method is an `import`-time
    `TypeError` naming what is missing rather than a `NotImplementedError` on a
    live request. Runs once per subclass; nothing per request consults it.
    """
    missing = [
        name
        for name in names
        if getattr(cls, name, None) is getattr(base, name, None)
        and name not in getattr(cls, "__abstractmethods__", ())
    ]
    if missing:
        raise TypeError(
            f"{cls.__name__} does not implement {base.__name__}: {', '.join(missing)} missing"
        )


def _quote_header_value(value: str) -> str:
    """Escape a string for the inside of an HTTP quoted-string.

    RFC 9110 Sec. 5.6.4: a backslash begins a quoted-pair, so both the
    backslash and the double-quote must be escaped - and the backslash first,
    or the backslash written to escape a quote would itself be doubled.

    Shared by every quoted-string producer in the package (header parameters,
    `Content-Disposition` filenames, `WWW-Authenticate` params and the test
    client's multipart headers), which previously each wrote the transform out
    and did not all get it right.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')
