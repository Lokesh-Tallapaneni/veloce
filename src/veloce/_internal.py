"""Semi-public internal utilities - shared across subpackages, not part of the public API.

The codebase guardrail in ``.claude/rules/development-guardrails.md`` under
"Cross-Subpackage Imports" forbids importing underscore-prefixed symbols
across subpackage boundaries. This module is the documented carve-out:
symbols defined here (``_reject_header_crlf``, ``_file_etag``, ``_b64encode``,
``_is_async_callable``, ``_extract_host``, and the MIME / status-phrase
constants) ARE permitted to be imported from any subpackage -
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

import base64
import contextlib
import hashlib
import inspect
import sys
import weakref
from collections.abc import Callable
from email.header import Header
from http import HTTPStatus
from typing import Any

from veloce._constants import (
    MIME_JSON as MIME_JSON,
)
from veloce._constants import (
    MIME_OCTET_STREAM,
    MIME_TEXT_HTML_UTF8,
    MIME_TEXT_PLAIN_UTF8,
    MSG_LABEL_HEADER_NAME,
    MSG_LABEL_SET_COOKIE_VALUE,
)
from veloce._protocol_constants import SET_COOKIE_JOINER

MIME_HTML = MIME_TEXT_HTML_UTF8
MIME_PLAIN = MIME_TEXT_PLAIN_UTF8
MIME_OCTET = MIME_OCTET_STREAM

# Reason-phrase lookup - `HTTPStatus(code).phrase` walks the IntEnum on
# every access. Build the mapping once at import time.
_STATUS_PHRASES: dict[int, str] = {s.value: s.phrase for s in HTTPStatus}


def _reject_header_crlf(value: str, what: str) -> str:
    """Reject CR, LF, or NUL in a header field name or value.

    Untrusted data carrying these characters enables HTTP response
    splitting / header injection. Raising - rather than silently
    stripping - surfaces the bug at the offending call site.
    """
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"{what} contains an illegal control character (CR, LF, or NUL)")
    return value


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
) -> list[str]:
    """Build the HTTP/1.1 status line plus header lines for a response.

    Shared by ``Response.encode()``, ``StreamingResponse.encode()`` and
    ``EventSourceResponse.stream_to()`` so the three raw-transport heads
    stay in lock-step. ``default_headers`` are framework defaults applied
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

    user_keys_lc = {k.lower() for k in headers}
    for name, value in default_headers.items():
        if name.lower() not in user_keys_lc:
            _reject_header_crlf(value, f"{name} header value")
            parts.append(f"{name}: {_encode_header_value(value)}\r\n")

    for key, value in headers.items():
        if key.lower() == "set-cookie":
            # One `Set-Cookie` dict entry may carry several cookies joined
            # by the internal separator; emit and CRLF-validate each line.
            for line in str(value).split(SET_COOKIE_JOINER):
                _reject_header_crlf(line, MSG_LABEL_SET_COOKIE_VALUE)
                parts.append(f"Set-Cookie: {_encode_header_value(line)}\r\n")
        else:
            _reject_header_crlf(str(key), MSG_LABEL_HEADER_NAME)
            sval = str(value)
            _reject_header_crlf(sval, f"{key} header value")
            parts.append(f"{key}: {_encode_header_value(sval)}\r\n")
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


def _extract_host(raw: str) -> str:
    """Strip port from a Host header value, handling IPv6 brackets."""
    if "[" in raw:
        return raw.split("]", 1)[0][1:].lower()
    if raw.count(":") >= 2:
        return raw.lower()
    return raw.split(":", 1)[0].lower()
