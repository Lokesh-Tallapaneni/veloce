r"""Header parsing — quoted-string-aware walker for HTTP header parameter lists.

Three headers in the codebase need the same walk with different settings:
`Content-Disposition` (`;`-separated, unescape on), RFC 7616 Digest field lists
(`,`-separated, unescape on), and the fallback `Authorization` param split
(`,`-separated, quotes preserved). Parametrising one walker rather than writing
three keeps them from drifting on the details that are easy to omit - a
tokenizer that skips backslash escapes reads `name="a\\\"b"` correctly for
two of the three and corrupts it for the third.

`parse_header_params` is the single walker the three call sites now
share. Semantics:

* Splits `value` on `delimiter` (`;` for Content-Disposition,
  `,` for Authorization / Digest) *outside* of double-quoted strings.
* If `unescape=True`, a backslash inside a quoted string consumes the
  next character verbatim - `\"` becomes `"`, `\\` becomes `\`. This
  is RFC 5322 / RFC 7616 quoted-pair behaviour.
* If `unescape=False`, the walker still advances past `\<char>` so the
  escape does not falsely terminate the quoted string, but the `\`
  and the escaped character are both emitted literally. The
  `Authorization.from_header` path uses this for back-compat with
  callers that already `.strip('"')` the result themselves.
* The surrounding double-quote characters are *always* stripped from
  values - both walkers we replaced did this either explicitly
  (Digest's `value[j + 1 : end]` slice) or implicitly (by skipping the
  `"` in the buf-append step).
* Returns `(prefix, params)` where `prefix` is the first token if it
  has no `=` (e.g. the disposition type `"form-data"`). When every
  token is `key=value` - the Digest and Authorization cases - `prefix`
  is `""`. Tokens without `=` *after* the first are dropped, matching
  the prior `if not token or "=" not in token: continue` behaviour.
* Parameter keys are lowercased and stripped. Empty keys are dropped.
* Values that came from a quoted string preserve their inner whitespace
  verbatim (the original Content-Disposition walker did, and the Digest
  walker sliced out the quoted span without strip). Values that were
  fully unquoted are stripped of surrounding whitespace, matching the
  Digest walker's `value[j:end].strip()` step.

Three narrower helpers sit beside it, sharing the same quoted-string rules:
`parse_media_type_params` walks a media-type parameter list (the portion of a
`Content-Type` after the bare type), `split_outside_quotes` splits on a
delimiter that a quoted string may contain, and `unquote_value` trims
whitespace and one surrounding pair of double quotes.

Everything here is module-internal (leading underscore on the module name); no
public re-export.
"""

from __future__ import annotations

from collections.abc import Iterator


def parse_media_type_params(rest: str) -> Iterator[tuple[str, str]]:
    """Yield `(lowercased key, unquoted value)` for a media-type parameter list.

    `rest` is the portion of a `Content-Type` / media-range value after the bare
    media type (everything following the first `;`). Single source for the
    `Content-Type` parsers on `Request`, `Response`, and the `Accept`
    media-range key.

    A parameter value may be a quoted-string, and a quoted-string may contain
    the `;` that otherwise separates parameters (RFC 9110 Sec. 5.6.4-5.6.6). A
    plain `split(";")` cut such a value short and left the opening quote on
    what survived, so `profile="a;b"` arrived as `"a` - which also made these
    accessors disagree with `parse_header_params`, the walker every other
    header parser in the framework reads a quoted value with.

    With no `"` anywhere there is no quoted region to hide a separator or an
    escape in, so the split and the walker agree by construction and the split
    is taken: that is the shape of nearly every `Content-Type`. A value that
    does carry a quote goes through the walker, which stays the one place
    quoting and escaping are interpreted.
    """
    if '"' not in rest:
        for chunk in rest.split(";"):
            key, eq, value = chunk.partition("=")
            if not eq:
                continue
            key = key.strip().lower()
            # An empty parameter name is not a token, so there is no parameter
            # here to report - the walker drops it too.
            if key:
                yield key, value.strip()
        return
    _, params = parse_header_params(rest, delimiter=";", unescape=True)
    yield from params.items()


def unquote_value(value: str) -> str:
    """Trim surrounding whitespace then a single pair of double quotes.

    The `.strip().strip('"')` idiom for recovering a header parameter value
    (a cookie value, a Cache-Control directive value, a `charset=` or
    `boundary=` parameter, a `Forwarded` element value). Whitespace is removed
    first so a quoted value padded with spaces (` "v" `) unquotes to `v`. The
    single source for the sites that do not need the full quoted-string walker.
    """
    return value.strip().strip('"')


def split_outside_quotes(value: str, delimiter: str) -> list[str]:
    """Split `value` on `delimiter`, but never inside a double-quoted string.

    Walks `value` once tracking an `in_quotes` flag. A `\\<char>` escape is
    skipped **only inside a quoted string**, which is the whole of what RFC 9110
    Sec. 5.6.4 defines `quoted-pair` for: outside one a backslash is an ordinary
    octet and must not hide the following delimiter. Honouring it everywhere
    lets a sender suppress a delimiter the rest of the stack still sees, and
    `Forwarded:` reaches here attacker-supplied.

    Returns the raw (still-quoted) substrings WITHOUT stripping quotes or
    whitespace - callers do that. `parse_header_params` walks the same grammar
    with the same rule; `tests/test_header_walker_agreement.py` holds them to it
    rather than a comment claiming they agree.
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
    return parts


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
    # Per token we record (raw, first_quote_open, last_quote_close) as indices
    # into the raw token. Whitespace before the first index and after the last
    # is *outside* any quoted region and is safe to trim; whitespace between
    # them was quoted and is part of the value. `-1` means the token never
    # entered a quoted region.
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
            # Fully-unquoted value: trim surrounding whitespace, matching
            # the original Digest walker's `value[j:end].strip()`.
            params[key] = val.strip()
        else:
            # The value may carry unquoted whitespace on either side of the
            # quoted region. What was quoted (between `quote_open` and
            # `quote_close` in the raw token) is preserved verbatim; the
            # whitespace outside it was never part of the value and is
            # trimmed, symmetrically on both sides.
            offset = len(raw_token) - len(val)
            head_end = max(quote_open - offset, 0)
            tail_start = max(quote_close - offset, 0)
            quoted = val[head_end:tail_start]
            params[key] = val[:head_end].lstrip() + quoted + val[tail_start:].rstrip()
    return prefix, params
