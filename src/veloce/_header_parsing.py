r"""Header parsing — quoted-string-aware walker for HTTP header parameter lists.

Three different headers in the codebase used to ship near-identical
ad-hoc tokenizers: `Content-Disposition` (`;`-separated, unescape on),
RFC 7616 Digest field lists (`,`-separated, unescape on), and the
fallback `Authorization` param split (`,`-separated, quotes preserved).
The three drifted: only Content-Disposition and Digest honoured
backslash escapes, so `name="a\\\"b"` round-tripped correctly for
multipart parts and Digest credentials but corrupted in
`Authorization.from_header`.

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

The helper is module-internal (leading underscore on the module name);
no public re-export.
"""

from __future__ import annotations


def unquote_value(value: str) -> str:
    """Trim surrounding whitespace then a single pair of double quotes.

    The `.strip().strip('"')` idiom used to recover a header parameter
    value (cookie value, Cache-Control directive value, a `charset=`/
    `boundary=` parameter, a `Forwarded` element value). Whitespace is
    removed first so a quoted value padded with spaces (` "v" `) unquotes
    to `v`. Single source for the sites that do not need the full
    quoted-string walker.
    """
    return value.strip().strip('"')


def split_outside_quotes(value: str, delimiter: str) -> list[str]:
    """Split `value` on `delimiter`, but never inside a double-quoted string.

    Walks `value` once tracking an `in_quotes` flag; a `\\<char>` escape is
    skipped so an escaped quote or delimiter never terminates a token. Returns
    the raw (still-quoted) substrings WITHOUT stripping quotes or whitespace -
    callers do that. Mirrors `parse_header_params`' inner escape handling so the
    two walkers stay consistent.
    """
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
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
    # Per token we record (raw, last_quote_close_index_in_raw). Whitespace
    # after that index is *outside* any quoted region and is safe to rstrip.
    # `-1` means the token never entered a quoted region.
    tokens: list[tuple[str, int]] = []
    buf: list[str] = []
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
            i += 1
            continue
        if ch == delimiter:
            tokens.append(("".join(buf), last_quote_close))
            buf = []
            last_quote_close = -1
            i += 1
            continue
        buf.append(ch)
        i += 1
    tokens.append(("".join(buf), last_quote_close))

    params: dict[str, str] = {}
    prefix = ""
    first = True
    for raw_token, quote_close in tokens:
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
            # The value may have unquoted whitespace before/after the
            # quoted region. The quoted region (up to `quote_close` in
            # the raw token) is preserved verbatim; trailing whitespace
            # added after the closing `"` is unquoted and stripped.
            offset = len(raw_token) - len(val)
            tail_start_in_val = quote_close - offset
            if tail_start_in_val < 0:
                tail_start_in_val = 0
            head = val[:tail_start_in_val]
            tail = val[tail_start_in_val:]
            params[key] = head + tail.rstrip()
    return prefix, params
