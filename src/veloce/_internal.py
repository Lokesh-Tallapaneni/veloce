"""Semi-public internal utilities — shared across subpackages, not part of the public API.

The codebase guardrail in ``.claude/rules/development-guardrails.md`` under
"Cross-Subpackage Imports" forbids importing underscore-prefixed symbols
across subpackage boundaries. This module is the documented carve-out:
symbols defined here (``_reject_header_crlf``, ``_file_etag``, ``_b64encode``,
``_is_async_callable``, ``_extract_host``, and the MIME / status-phrase
constants) ARE permitted to be imported from any subpackage —
``http/``, ``middleware/``, ``security/``, ``serving/``, ``contrib/``,
``routing/`` — because they are explicitly internal-to-the-framework
helpers with a stable contract.

The leading underscore signals "not for users"; this module's existence
and docstring signal "stable for internal use across the framework".
External users must not depend on these symbols — they are not in
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
import weakref
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

MIME_JSON = "application/json"
MIME_HTML = "text/html; charset=utf-8"
MIME_PLAIN = "text/plain; charset=utf-8"
MIME_OCTET = "application/octet-stream"

# Reason-phrase lookup — `HTTPStatus(code).phrase` walks the IntEnum on
# every access. Build the mapping once at import time.
_STATUS_PHRASES: dict[int, str] = {s.value: s.phrase for s in HTTPStatus}


def _reject_header_crlf(value: str, what: str) -> str:
    """Reject CR, LF, or NUL in a header field name or value.

    Untrusted data carrying these characters enables HTTP response
    splitting / header injection. Raising — rather than silently
    stripping — surfaces the bug at the offending call site.
    """
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"{what} contains an illegal control character (CR, LF, or NUL)")
    return value


def _file_etag(path: str, size: int, mtime: float) -> str:
    """Weak, opaque-quoted ETag derived from (path, size, mtime).

    RFC 9110 §8.8.3 — entity-tags must be marked weak (`W/`) when they
    do not guarantee byte-for-byte identity. mtime-derived tags can
    collide within the same second across content-altering writes, so
    a weak validator is the only spec-compliant choice. Weak compare
    (§8.8.3.2) still lets `If-None-Match` / `If-Range` work for cache
    revalidation; strict `If-Match` correctly refuses these tags.
    """
    key = f"{path}:{size}:{mtime}".encode()
    return f'W/"{hashlib.md5(key).hexdigest()}"'


def _etag_matches_weak(server_etag: str, client_token: str) -> bool:
    """Weak comparison of two ETag tokens per RFC 9110 §8.8.3.2.

    Strips surrounding whitespace, an optional `W/` weak marker, and
    the surrounding double quotes on both sides before comparing the
    opaque tags. Returns True when the opaque-tags are equal regardless
    of weak/strong marking — required for `If-None-Match` and `If-Range`
    against weak server validators.
    """
    a = server_etag.strip().removeprefix("W/").strip('"')
    b = client_token.strip().removeprefix("W/").strip('"')
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
    """Memoised `inspect.iscoroutinefunction` for hot-path hook dispatch."""
    try:
        cached = _iscoro_cache.get(fn)
    except TypeError:
        return inspect.iscoroutinefunction(fn)
    if cached is not None:
        return cached
    result = inspect.iscoroutinefunction(fn)
    with contextlib.suppress(TypeError):
        _iscoro_cache[fn] = result
    return result


def _extract_host(raw: str) -> str:
    """Strip port from a Host header value, handling IPv6 brackets."""
    if "[" in raw:
        return raw.split("]", 1)[0][1:].lower()
    if raw.count(":") >= 2:
        return raw.lower()
    return raw.split(":", 1)[0].lower()
