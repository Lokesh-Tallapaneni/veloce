"""Shared internal utilities — not part of the public API."""

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
    """Strong, opaque-quoted ETag derived from (path, size, mtime).

    RFC 9110 §8.8.3 — the entity-tag is `quoted-string`. Using MD5 of
    the identity tuple keeps it deterministic across processes.
    """
    key = f"{path}:{size}:{mtime}".encode()
    return f'"{hashlib.md5(key).hexdigest()}"'


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
