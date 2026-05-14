"""Header value parsers `parse_options_header`,
`parse_set_header`, `dump_options_header`, `parse_etags`.

Each parser is grounded in the RFC that defines the header it reads.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


def parse_options_header(value: str) -> tuple[str, dict[str, str]]:
    """Split `Content-Type`-style headers into `(primary, options)`.

    Per RFC 9110 §5.6.6, structured field values often take the shape
    `value; option=value; option2="quoted value"`. This returns the
    bare primary (lower-cased) and a dict of parameter→value (keys
    lower-cased, quoted values unquoted).

    Examples
    --------
    >>> parse_options_header("text/html; charset=utf-8")
    ('text/html', {'charset': 'utf-8'})
    >>> parse_options_header('attachment; filename="report.pdf"')
    ('attachment', {'filename': 'report.pdf'})
    >>> parse_options_header("")
    ('', {})
    """
    if not value:
        return "", {}
    parts = value.split(";")
    primary = parts[0].strip().lower()
    options: dict[str, str] = {}
    for chunk in parts[1:]:
        chunk = chunk.strip()
        if "=" not in chunk:
            # Bare token in an options position — store with empty value.
            if chunk:
                options[chunk.lower()] = ""
            continue
        k, _, v = chunk.partition("=")
        k = k.strip().lower()
        v = v.strip()
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1]
        options[k] = v
    return primary, options


def dump_options_header(header: str, options: dict[str, str]) -> str:
    """Inverse of `parse_options_header`.

    Re-quotes any value containing whitespace, `;`, or `,`. Keys are
    emitted in caller-supplied dict order; the primary `header` is
    emitted as-is (not lower-cased — the caller controls casing on
    the way out).
    """
    parts = [header] if header else []
    for k, v in options.items():
        if v == "":
            parts.append(k)
            continue
        if any(c in v for c in (" ", ";", ",", "\t", '"')):
            v_out = '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        else:
            v_out = v
        parts.append(f"{k}={v_out}")
    return "; ".join(parts)


def parse_set_header(value: str) -> frozenset[str]:
    """Split a comma-separated header value into a frozenset of tokens.

    Use for headers like `Connection`, `Pragma`, or `Vary` where the
    spec defines the value as a comma-separated set. Tokens are
    stripped of surrounding whitespace and lower-cased. Empty input
    returns an empty set.
    """
    if not value:
        return frozenset()
    return frozenset(v.strip().lower() for v in value.split(",") if v.strip())


_ETAG_RE = re.compile(r'(?:W/)?"([^"]*)"')


def parse_etags(value: str) -> list[tuple[str, bool]]:
    """Parse an `If-Match` / `If-None-Match` / `ETag` header.

    Returns a list of `(tag, weak)` pairs in source order. The tag is
    the contents *inside* the surrounding quotes; `weak` is True if
    the tag was prefixed with `W/` (RFC 9110 §8.8.1). The bare value
    `*` (matches any entity) is returned as a single
    `[("*", False)]` entry.

    Empty input returns an empty list.
    """
    if not value:
        return []
    if value.strip() == "*":
        return [("*", False)]
    out: list[tuple[str, bool]] = []
    for match in _ETAG_RE.finditer(value):
        token = match.group(0)
        weak = token.startswith("W/")
        out.append((match.group(1), weak))
    return out


def _ensure_iterable(value: Iterable[str] | str | None) -> Iterable[str]:
    """Tolerant accessor — accept None, str, or any iterable of str."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return value
