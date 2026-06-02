"""Cookie string helpers - `parse_cookie` / `dump_cookie` (RFC 6265).

`parse_cookie` reads a `Cookie:` request-header value into a dict.
`dump_cookie` builds a `Set-Cookie:` response-header value from a
name/value pair plus the standard attributes. Both are derived from RFC 6265.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from urllib.parse import quote, unquote

from veloce._internal import _reject_header_crlf
from veloce.http.dates import http_date


def iter_cookies(header: str | None) -> Iterator[tuple[str, str]]:
    """Yield `(name, value)` pairs from a `Cookie:` header - RFC 6265 Sec. 5.4.

    Values are percent-decoded (the inverse of `dump_cookie`'s quoting).
    Segments without an `=` are skipped. When a name repeats, the first
    occurrence wins (browsers send the most specific cookie first), so
    later duplicates are not yielded.
    """
    if not header:
        return
    seen: set[str] = set()
    for chunk in header.split(";"):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        if name in seen:
            continue
        seen.add(name)
        value = value.strip().strip('"')
        yield name, unquote(value)


def parse_cookie(header: str | None) -> dict[str, str]:
    """Parse a `Cookie:` header into `{name: value}` - RFC 6265 Sec. 5.4.

    Values are percent-decoded (the inverse of `dump_cookie`'s quoting).
    Segments without an `=` are skipped. When a name repeats, the first
    occurrence wins (browsers send the most specific cookie first).
    """
    return dict(iter_cookies(header))


def dump_cookie(
    key: str,
    value: str = "",
    *,
    max_age: int | timedelta | None = None,
    expires: int | float | datetime | None = None,
    path: str | None = "/",
    domain: str | None = None,
    secure: bool = False,
    httponly: bool = False,
    samesite: str | None = None,
) -> str:
    """Build a `Set-Cookie:` header value - RFC 6265 Sec. 4.1.

    The cookie value is percent-quoted so control characters and the
    delimiters `;`, `,`, and whitespace can't break out of the
    attribute. Attribute rules:

    - `max_age` accepts an int (seconds) or `timedelta`.
    - `expires` accepts a POSIX timestamp or `datetime`; rendered as an
      IMF-fixdate via `http_date`.
    - `samesite` must be one of `Strict` / `Lax` / `None` (case-insensitive).
    """
    _reject_header_crlf(key, "cookie name")
    _reject_header_crlf(value, "cookie value")
    quoted = quote(value, safe="!#$%&'()*+/:<=>?@[]^`{|}~")
    parts: list[str] = [f"{key}={quoted}"]

    if max_age is not None:
        secs = int(max_age.total_seconds()) if isinstance(max_age, timedelta) else int(max_age)
        parts.append(f"Max-Age={secs}")

    if expires is not None:
        parts.append(f"Expires={http_date(expires)}")

    if path:
        _reject_header_crlf(path, "cookie path")
        parts.append(f"Path={path}")
    if domain:
        _reject_header_crlf(domain, "cookie domain")
        parts.append(f"Domain={domain}")
    if secure:
        parts.append("Secure")
    if httponly:
        parts.append("HttpOnly")
    if samesite is not None:
        _reject_header_crlf(samesite, "cookie samesite")
        normalised = samesite.strip().capitalize()
        if normalised not in ("Strict", "Lax", "None"):
            raise ValueError("samesite must be 'Strict', 'Lax', or 'None'")
        parts.append(f"SameSite={normalised}")

    return "; ".join(parts)
