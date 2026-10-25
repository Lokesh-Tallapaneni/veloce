r"""Header parsing — quoted-string-aware walker for HTTP header parameter lists.

`parse_header_params` is the single walker three call sites share:
`Content-Disposition` (`;`-separated, unescape on), RFC 7616 Digest field lists
(`,`-separated, unescape on), and the fallback `Authorization` param split
(`,`-separated, quotes preserved). Semantics:

* Splits `value` on `delimiter` *outside* of double-quoted strings.
* `unescape=True` applies RFC 5322 / RFC 7616 quoted-pair behaviour inside a
  quoted string - `\"` becomes `"`, `\\` becomes `\`. `unescape=False` still
  advances past `\<char>` so the escape cannot falsely terminate the quoted
  string, but emits the backslash and the escaped character literally.
* The surrounding double-quote characters are *always* stripped from values.
* Returns `(prefix, params)` where `prefix` is the first token if it has no `=`
  (e.g. the disposition type `"form-data"`) and `""` when every token is
  `key=value`. Tokens without `=` *after* the first are dropped.
* Parameter keys are lowercased and stripped. Empty keys are dropped.
* A value that came from a quoted string preserves its inner whitespace
  verbatim; a fully-unquoted value is stripped of surrounding whitespace.

Three narrower helpers sit beside it, sharing the same quoted-string rules:
`parse_media_type_params` walks a media-type parameter list (the portion of a
`Content-Type` after the bare type), `split_outside_quotes` splits on a
delimiter that a quoted string may contain, and `unquote_value` trims
whitespace and one surrounding pair of double quotes.
"""

from __future__ import annotations

from collections.abc import Iterator


def parse_media_type_params(rest: str) -> Iterator[tuple[str, str]]:
    """Yield `(lowercased key, unquoted value)` for a media-type parameter list.

    `rest` is the portion of a `Content-Type` / media-range value after the bare
    media type (everything following the first `;`). Single source for the
    `Content-Type` parsers on `Request`, `Response`, and the `Accept`
    media-range key.

    A parameter value may be a quoted-string containing the `;` that otherwise
    separates parameters (RFC 9110 Sec. 5.6.4-5.6.6), so a value carrying a `"`
    goes through `parse_header_params`. With no quote anywhere the split and the
    walker agree by construction, so the cheaper split is taken.
    """
    if '"' not in rest:
        for chunk in rest.split(";"):
            key, eq, value = chunk.partition("=")
            if not eq:
                continue
            key = key.strip().lower()
            # An empty parameter name is not a token; the walker drops it too.
            if key:
                yield key, value.strip()
        return
    _, params = parse_header_params(rest, delimiter=";", unescape=True)
    yield from params.items()


def unquote_value(value: str) -> str:
    """Trim surrounding whitespace then a single pair of double quotes.

    The single source for header-parameter sites that do not need the full
    quoted-string walker. Whitespace goes first, so ` "v" ` unquotes to `v`.
    """
    return value.strip().strip('"')


def split_outside_quotes(value: str, delimiter: str) -> list[str]:
    """Split `value` on `delimiter`, but never inside a double-quoted string.

    The terminal quote state is dropped here. A caller that must refuse a
    malformed header - one whose last quote is never closed - uses
    `split_outside_quotes_checked` instead.
    """
    return split_outside_quotes_checked(value, delimiter)[0]


def split_outside_quotes_checked(value: str, delimiter: str) -> tuple[list[str], bool]:
    """Split as `split_outside_quotes`, also reporting an unterminated quote.

    Walks `value` once tracking an `in_quotes` flag. A `\\<char>` escape is
    skipped **only inside a quoted string**, which is the whole of what RFC 9110
    Sec. 5.6.4 defines `quoted-pair` for: outside one a backslash is an ordinary
    octet and must not hide the following delimiter. Honouring it everywhere
    lets a sender suppress a delimiter the rest of the stack still sees, and
    `Forwarded:` reaches here attacker-supplied.

    Returns the raw (still-quoted) substrings WITHOUT stripping quotes or
    whitespace - callers do that.
    """
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if in_quotes and ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(value[i + 1])
            i += 2
            continue
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
            i += 1
            continue
        if ch == delimiter and not in_quotes:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    # `in_quotes` still set means the last quote was never closed, so every
    # delimiter after it was swallowed.
    return parts, in_quotes


def parse_header_params(
    value: str,
    *,
    delimiter: str = ";",
    unescape: bool = True,
) -> tuple[str, dict[str, str]]:
    """Walk a header value (Content-Disposition / Authorization / Digest).

    See module docstring for the full semantics. `delimiter` is the
    parameter separator outside quoted strings. `unescape=True` decodes
    `\\X` to `X` inside quoted strings; `unescape=False` preserves the
    backslash literally while still using it for quoted-string boundary
    detection.
    """
    # Per token: (raw, first_quote_open, last_quote_close), `-1` when it never
    # entered a quoted region. Whitespace inside that span is part of the value.
    tokens: list[tuple[str, int, int]] = []
    buf: list[str] = []
    first_quote_open = -1
    last_quote_close = -1
    in_quotes = False
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if in_quotes:
            if ch == "\\" and i + 1 < n:
                if unescape:
                    buf.append(value[i + 1])
                else:
                    buf.append(ch)
                    buf.append(value[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quotes = False
                last_quote_close = len(buf)
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_quotes = True
            if first_quote_open < 0:
                first_quote_open = len(buf)
            i += 1
            continue
        if ch == delimiter:
            tokens.append(("".join(buf), first_quote_open, last_quote_close))
            buf = []
            first_quote_open = -1
            last_quote_close = -1
            i += 1
            continue
        buf.append(ch)
        i += 1
    tokens.append(("".join(buf), first_quote_open, last_quote_close))

    params: dict[str, str] = {}
    prefix = ""
    first = True
    for raw_token, quote_open, quote_close in tokens:
        if "=" not in raw_token:
            stripped = raw_token.strip()
            if not stripped:
                first = False
                continue
            if first:
                prefix = stripped
            first = False
            continue
        first = False
        key, _, val = raw_token.partition("=")
        key = key.strip().lower()
        if not key:
            continue
        if quote_close < 0:
            # Fully-unquoted value: trim surrounding whitespace.
            params[key] = val.strip()
        else:
            # The quoted region is preserved verbatim; whitespace outside it was
            # never part of the value and is trimmed on both sides.
            offset = len(raw_token) - len(val)
            head_end = max(quote_open - offset, 0)
            tail_start = max(quote_close - offset, 0)
            quoted = val[head_end:tail_start]
            params[key] = val[:head_end].lstrip() + quoted + val[tail_start:].rstrip()
    return prefix, params
