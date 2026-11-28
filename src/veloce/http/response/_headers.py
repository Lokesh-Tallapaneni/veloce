"""Response header helpers — case-insensitive access and length advertisement."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from urllib.parse import quote

from veloce._internal import (
    _quote_header_value,
    _reject_header_crlf,
)
from veloce.status import (
    HTTP_304_NOT_MODIFIED,
    status_permits_body,
)

# ── Case-insensitive response-header access ──────────────────────────
# `Response.headers` is case-SENSITIVE but HTTP field names are not (RFC 9110
# Sec. 5.1), so these fold the lookup without allocating a dict per call.


def header_key(headers: Mapping[str, str], name: str) -> str | None:
    """Return the actual stored key matching `name` case-insensitively, or None.

    Pass `name` in its canonical casing. Use the returned key to rewrite a
    value in place under whatever casing the caller originally stored.
    """
    if name in headers:
        return name
    lowered = name.lower()
    # A first-byte guard before the `.lower()` measured slower on a short dict.
    for key in headers:
        if key.lower() == lowered:
            return key
    return None


def header_get(headers: Mapping[str, str], name: str) -> str | None:
    """Return the value stored under `name` case-insensitively, or None."""
    key = header_key(headers, name)
    return None if key is None else headers[key]


def header_present(headers: Mapping[str, str], name: str) -> bool:
    """Return True when a header named `name` exists under any casing."""
    return header_key(headers, name) is not None


def header_pop(headers: MutableMapping[str, str], name: str) -> str | None:
    """Remove and return `name` under whatever casing it was stored, or None.

    Costs one dict lookup for the canonical casing, which is what the framework
    itself always writes.
    """
    key = header_key(headers, name)
    return None if key is None else headers.pop(key)


def _format_content_disposition(disposition: str, filename: str) -> str:
    """Build a safe RFC 6266 ``Content-Disposition`` header value.

    An ASCII name made only of RFC 9110 quoted-string members (HTAB, SP,
    0x21-0x7E) is emitted as ``filename="..."``; any other name is emitted only
    as the RFC 5987 ``filename*=UTF-8''...`` form, with no lossy legacy slot. A
    CR/LF in the name is rejected so it cannot inject a header.
    """
    _reject_header_crlf(filename, "filename")
    quotable = filename.isascii() and all(
        c == "\t" or c == " " or "\x21" <= c <= "\x7e" for c in filename
    )
    if quotable:
        escaped = _quote_header_value(filename)
        param = f'filename="{escaped}"'
    else:
        param = f"filename*=UTF-8''{quote(filename, safe='')}"
    return f"{disposition}; {param}"


def advertised_length(status_code: int, body: bytes) -> int | None:
    """Return the `Content-Length` to synthesize, or `None` when it is unknown.

    A body-permitting status advertises what it carries. A 304 may advertise the
    length the equivalent 200 would have carried (RFC 9110 Sec. 8.6 / 15.4.5),
    but only when that is knowable; an unknown length is omitted rather than
    reported as 0, because RFC 9111 Sec. 4.3.4 has caches write a 304's length
    over their stored entry and a guessed 0 records a 200 KiB asset as empty.
    Every other bodiless status (1xx, 204, 205) has no representation, so its
    length is 0.
    """
    if status_permits_body(status_code):
        return len(body)
    if status_code == HTTP_304_NOT_MODIFIED:
        return len(body) or None
    return 0


# Merged `Vary` values keyed by `(existing_header, names_added)`; a pure merge.
_VARY_MERGES: dict[tuple[str, tuple[str, ...]], str] = {}


_MAX_VARY_MERGES = 256
