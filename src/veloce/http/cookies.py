"""Cookie string helpers — `parse_cookie` / `dump_cookie` (RFC 6265).

`parse_cookie` reads a `Cookie:` request-header value into a dict.
`dump_cookie` builds a `Set-Cookie:` response-header value from a
name/value pair plus the standard attributes. Both are derived from RFC 6265.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import quote, unquote

from veloce._constants import (
    MSG_LABEL_COOKIE_DOMAIN,
    MSG_LABEL_COOKIE_NAME,
    MSG_LABEL_COOKIE_PATH,
    MSG_LABEL_COOKIE_SAMESITE,
    MSG_LABEL_COOKIE_VALUE,
)
from veloce._header_parsing import unquote_value
from veloce._internal import _reject_header_crlf
from veloce.http.dates import http_date

# RFC 6265 Sec. 4.1.1: cookie-name = token (RFC 7230 Sec. 3.2.6) - the VCHAR set
# minus CTLs and the separators ()<>@,;:\"/[]?={} plus SP/HT. One-or-more, so an
# empty name fails. Written from the spec, not from any framework's char table.
_COOKIE_NAME_TOKEN = re.compile(r"[!#$%&'*+\-.0-9A-Z^_`a-z|~]+").fullmatch

# Cookie-attribute keywords (lowercased). A cookie name equal to one of these
# can be misread as a Set-Cookie attribute by a lenient parser, so reject it.
_RESERVED_COOKIE_NAMES = frozenset(
    {"expires", "max-age", "domain", "path", "secure", "httponly", "samesite", "partitioned"}
)


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
        # `partition` already reports whether a separator was there, so the
        # separate membership test - a second scan of every chunk - is not
        # needed to skip a segment that carries none.
        name, eq, value = chunk.partition("=")
        if not eq:
            continue
        name = name.strip()
        if name in seen:
            continue
        seen.add(name)
        value = value.strip()
        # `dump_cookie` percent-encodes and optionally quotes; a value carrying
        # neither marker is already what it decodes to. The strip above has to
        # stay for this branch, since `unquote_value` is what does it otherwise.
        if "%" in value or '"' in value:
            value = unquote(unquote_value(value))
        yield name, value


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
    prefix: Literal["host", "secure"] | None = None,
) -> str:
    """Build a `Set-Cookie:` header value - RFC 6265 Sec. 4.1.

    The cookie value is percent-quoted so control characters and the
    delimiters `;`, `,`, and whitespace can't break out of the
    attribute. Attribute rules:

    - `max_age` accepts an int (seconds) or `timedelta`.
    - `expires` accepts a POSIX timestamp or `datetime`; rendered as an
      IMF-fixdate via `http_date`.
    - `samesite` must be one of `Strict` / `Lax` / `None` (case-insensitive).
    - `prefix` adds an RFC 6265bis Sec. 4.1.3 cookie-name prefix and enforces
      its invariants: `"secure"` requires `secure=True` and emits the wire name
      `__Secure-<key>`; `"host"` additionally requires `path="/"` and no
      `domain`, emitting `__Host-<key>`. The bare `key` is validated; the
      framework-controlled prefix is prepended to the wire name afterwards.
    """
    if prefix is not None:
        # RFC 6265bis Sec. 4.1.3 - validate against the BARE key, then derive
        # the wire name. The `__Host-`/`__Secure-` literal is framework-owned
        # and already conforms, so it skips the token/reserved-name checks.
        if prefix == "secure":
            if secure is not True:
                raise ValueError("__Secure- cookie prefix requires secure=True")
            wire_key = f"__Secure-{key}"
        elif prefix == "host":
            if secure is not True:
                raise ValueError("__Host- cookie prefix requires secure=True")
            if path != "/":
                raise ValueError("__Host- cookie prefix requires path='/'")
            if domain is not None:
                raise ValueError("__Host- cookie prefix requires domain=None")
            wire_key = f"__Host-{key}"
        else:
            raise ValueError("cookie prefix must be 'host', 'secure', or None")
    else:
        wire_key = key
    _reject_header_crlf(key, MSG_LABEL_COOKIE_NAME)
    # The name must be a valid RFC 6265 token (no spaces/separators/CTLs) and
    # must not collide with a cookie-attribute keyword - both prevent a
    # malformed or attribute-injecting Set-Cookie header.
    if not _COOKIE_NAME_TOKEN(key):
        raise ValueError(
            f"cookie name {key!r} is not a valid RFC 6265 token "
            "(must avoid spaces, separators, and control characters)"
        )
    if key.lower() in _RESERVED_COOKIE_NAMES:
        raise ValueError(f"cookie name {key!r} collides with a reserved cookie-attribute keyword")
    _reject_header_crlf(value, MSG_LABEL_COOKIE_VALUE)
    # `%` must NOT be in the safe set: it is the percent-encoding marker, and
    # `parse_cookie` percent-decodes the value. Leaving a literal `%` unescaped
    # makes a value like "100%" decode back to garbage (e.g. "%00" -> NUL), so
    # the value would not survive a dump -> parse round-trip. Encoding it as
    # "%25" is the inverse `unquote` restores.
    quoted = quote(value, safe="!#$&'()*+/:<=>?@[]^`{|}~")
    parts: list[str] = [f"{wire_key}={quoted}"]

    if max_age is not None:
        secs = int(max_age.total_seconds()) if isinstance(max_age, timedelta) else int(max_age)
        parts.append(f"Max-Age={secs}")

    if expires is not None:
        parts.append(f"Expires={http_date(expires)}")

    if path:
        _reject_header_crlf(path, MSG_LABEL_COOKIE_PATH)
        parts.append(f"Path={path}")
    if domain:
        _reject_header_crlf(domain, MSG_LABEL_COOKIE_DOMAIN)
        parts.append(f"Domain={domain}")
    if secure:
        parts.append("Secure")
    if httponly:
        parts.append("HttpOnly")
    if samesite is not None:
        _reject_header_crlf(samesite, MSG_LABEL_COOKIE_SAMESITE)
        # The whole rule lives here, in the one function that renders a
        # `Set-Cookie`. Callers used to fix the value up on the way in - one
        # dropped a whitespace-only value, one capitalised, one passed the raw
        # string through - so a value this rejects reached it from one caller
        # and not another: `samesite="  "` made the cookie backend raise on
        # every response while the server-side one shipped a cookie with no
        # `SameSite` at all. Normalising inside the serialiser means there is no
        # "on the way in" for the copies to disagree about.
        normalised = samesite.strip().capitalize()
        if not normalised:
            return "; ".join(parts)
        if normalised not in ("Strict", "Lax", "None"):
            raise ValueError("samesite must be 'Strict', 'Lax', or 'None'")
        parts.append(f"SameSite={normalised}")

    return "; ".join(parts)
