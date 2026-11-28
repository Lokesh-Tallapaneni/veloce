"""Parsed header values — `RangeSpec`, `AcceptHeader` and `Authorization`."""

from __future__ import annotations

import functools
import math
from collections.abc import Iterator

from veloce._header_parsing import parse_header_params, parse_media_type_params
from veloce._internal import _decode_basic_credentials


# ── Parsed header values ──────────────────────────────────
def _is_byte_pos(value: str) -> bool:
    """True when `value` is a byte position - ASCII digits only, nothing else."""
    return value.isdigit() and value.isascii()


class RangeSpec:
    """Parsed `Range:` header (RFC 9110 Sec. 14.2).

    - `unit` is the range unit, e.g. `"bytes"` (the only commonly-used one).
    - `ranges` is a list of `(start, end)` tuples, with `None` standing in
      for an open endpoint:
        - `0-499`   -> `(0, 499)`
        - `1000-`   -> `(1000, None)` (open at the right)
        - `-500`    -> `(None, 500)`  (suffix-range - last 500 bytes)
    """

    __slots__ = ("unit", "ranges")

    def __init__(self, unit: str, ranges: list[tuple[int | None, int | None]]) -> None:
        self.unit = unit
        self.ranges = ranges

    @classmethod
    def parse(cls, header_value: str) -> RangeSpec | None:
        """Parse a `Range:` header, returning `None` for a missing or unparseable value."""
        if not header_value:
            return None
        if "=" not in header_value:
            return None
        unit, _, spec = header_value.partition("=")
        unit = unit.strip().lower()
        ranges: list[tuple[int | None, int | None]] = []
        for chunk in spec.split(","):
            chunk = chunk.strip()
            if not chunk or "-" not in chunk:
                continue
            start_s, _, end_s = chunk.partition("-")
            start_s = start_s.strip()
            end_s = end_s.strip()
            # RFC 9110 Sec. 14.1.1 spells a byte position `1*DIGIT`, where
            # `int()` also reads a sign, `_` separators and non-ASCII digits.
            if (start_s and not _is_byte_pos(start_s)) or (end_s and not _is_byte_pos(end_s)):
                continue
            if not start_s and not end_s:
                continue  # `-` alone is invalid
            ranges.append((int(start_s) if start_s else None, int(end_s) if end_s else None))
        if not ranges:
            return None
        return cls(unit=unit, ranges=ranges)

    def __repr__(self) -> str:
        return f"RangeSpec(unit={self.unit!r}, ranges={self.ranges!r})"


# Lower-cased type, subtype, the frozenset of media-type parameter pairs, and
# a specificity score ranking competing matches (RFC 9110 Sec. 12.5.1):
#   3 = `type/subtype` with parameters, 2 = `type/subtype`, 1 = `type/*`,
#   0 = `*/*`, -1 = invalid (e.g. `*/json`), which never matches.
_MimeKey = tuple[str, str, frozenset[tuple[str, str]], int]


@functools.lru_cache(maxsize=128)
def _lookup_mime_key(value: str) -> _MimeKey:
    """`_parse_mime_key` for a media range the caller names, memoized.

    `quality("application/json")` asks about a literal the application owns,
    and re-parsing it per call measured three parses on one MCP request.
    Deliberately NOT used for the ranges parsed out of an `Accept` header: those
    are remote input, and a few hundred distinct ones would evict the handful
    this cache exists to hold.
    """
    return _parse_mime_key(value)


def _parse_mime_key(value: str) -> _MimeKey:
    """Decompose a media range into a comparison-ready `_MimeKey`."""
    head, _, rest = value.partition(";")
    head = head.strip().lower()
    type_, slash, subtype = head.partition("/")
    if not slash:
        # No `/` - not a valid media range.
        return ("", "", frozenset(), -1)
    type_ = type_.strip()
    subtype = subtype.strip()
    # `*/non-*` is meaningless (RFC 9110 Sec. 12.5.1) - never match it.
    if type_ == "*" and subtype != "*":
        return ("", "", frozenset(), -1)
    # Parameter names are case-insensitive; values keep all but their quotes.
    params: set[tuple[str, str]] = set(parse_media_type_params(rest))
    if type_ == "*":
        specificity = 0
    elif subtype == "*":
        specificity = 1
    elif params:
        specificity = 3
    else:
        specificity = 2
    return (type_, subtype, frozenset(params), specificity)


class AcceptHeader:
    """Parsed `Accept-*` header with RFC 9110 Sec. 12.5 q-value semantics.

    Construction is via `AcceptHeader.parse(raw, mime=False)`. `mime=True`
    enables MIME-style wildcard matching (`text/*`, `*/*`) used by
    `Accept`; defaults to plain string equality used by `Accept-Language`,
    `Accept-Encoding`, `Accept-Charset`.
    """

    __slots__ = ("_options", "_mime")

    def __init__(self, options: list[tuple[str, float]], *, mime: bool) -> None:
        # Never re-sorted: `best_match` breaks ties on the configured option
        # order. A MIME option is decomposed once into a cached `_MimeKey`.
        self._mime = mime
        self._options: list[tuple[str, float, _MimeKey | None]]
        if mime:
            self._options = [(value, q, _parse_mime_key(value)) for value, q in options]
        else:
            self._options = [(value, q, None) for value, q in options]

    # ── Parsing ────────────────────────────────────────────

    @classmethod
    def parse(cls, raw: str, *, mime: bool = False) -> AcceptHeader:
        """Parse a comma-separated header into (value, q) tuples.

        A missing or unparseable q-value defaults to 1.0 (RFC 9110
        Sec. 12.4.2). An entry with `q=0` is kept, because `best_match` treats
        it as an explicit rejection. For MIME headers the media-type parameters
        are retained and participate in matching (RFC 9110 Sec. 12.5.1).

        Args:
            raw: The header value; an empty string yields an empty header.
            mime: Treat entries as media types, keeping their parameters for matching.

        Returns:
            The parsed header.

        """
        if not raw:
            return cls([], mime=mime)
        items: list[tuple[str, float]] = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ";" not in chunk:
                items.append((chunk, 1.0))
                continue
            value, _, rest = chunk.partition(";")
            value = value.strip()
            q = 1.0
            # Media-type parameters appear BEFORE `q` and accept-extension ones
            # after (RFC 9110 Sec. 12.5.1); only the former take part in matching.
            params: list[str] = []
            seen_q = False
            for param in rest.split(";"):
                param = param.strip()
                if not param:
                    continue
                if not seen_q and (param.startswith("q=") or param.startswith("Q=")):
                    try:
                        q = float(param[2:])
                    except ValueError:
                        q = 1.0
                    # A non-finite q from `float("inf")` would poison every
                    # comparison, so it is treated as unparseable.
                    if not math.isfinite(q):
                        q = 1.0
                    seen_q = True
                    continue
                if not seen_q:
                    params.append(param)
            if mime and params:
                value = value + ";" + ";".join(params)
            items.append((value, q))
        return cls(items, mime=mime)

    # ── Quality queries ────────────────────────────────────

    @property
    def values(self) -> list[str]:
        """All accepted values in the order the client sent them."""
        return [v for v, _, _ in self._options]

    def quality(self, value: str) -> float:
        """Return the q-value the client assigned to `value`.

        For MIME headers, matches `*/*` and `type/*` wildcards as well as
        parameterized media ranges; the MOST SPECIFIC matching client range wins
        (RFC 9110 Sec. 12.5.1), ties broken by the higher q-value. Returns 0 for
        a value that is rejected or not mentioned.
        """
        if self._mime:
            return self._mime_best(_lookup_mime_key(value))[0]
        folded = value.lower()
        best = 0.0
        for opt, q, _okey in self._options:
            if (opt.lower() == folded or opt == "*") and q > best:
                best = q
        return best

    def quality_explicit(self, value: str) -> float:
        """Return the q-value for `value`, with explicit tokens overriding `*`.

        RFC 9110 Sec. 12.5.3: an explicit `q=0` means "not acceptable" and must
        override a more permissive wildcard, which `quality()` does not do - it
        reports the MAX, so `br;q=0, *;q=1` gives `br` 1.0. This variant prefers
        an EXACT token match and falls back to the `*` q only for a value not
        explicitly listed. Non-MIME (Accept-Encoding) semantics.
        """
        # Content-coding tokens are case-insensitive (RFC 9110 Sec. 12.5.3 /
        # Sec. 8.4.1), so an explicit `Br;q=0` must reject `br`.
        folded = value.lower()
        # RFC 9110 leaves a repeated token with conflicting weights
        # (`gzip;q=1.0, gzip;q=0`) undefined; an explicit refusal anywhere wins.
        found: float | None = None
        for opt, q, _okey in self._options:
            if opt.lower() == folded:
                if q == 0.0:
                    return 0.0
                if found is None:
                    found = q
        if found is not None:
            return found
        for opt, q, _okey in self._options:
            if opt == "*":
                return q
        return 0.0

    def accepts_identity(self) -> bool:
        """Whether the `identity` (no-encoding) coding is acceptable per RFC 9110.

        RFC 9110 Sec. 12.5.3: `identity` is acceptable by default. It is
        UNacceptable only when an explicit `identity` entry carries `q=0`, or
        when it is unlisted and a `*` entry carries `q=0`. Token comparison is
        case-insensitive (Sec. 8.4.1).
        """
        for opt, q, _okey in self._options:
            if opt.lower() == "identity":
                # Explicit entry wins: identity is acceptable iff its q > 0.
                return q > 0
        for opt, q, _okey in self._options:
            if opt == "*":
                # Identity not named; the wildcard's q decides it.
                return q > 0
        # Neither identity nor `*` listed - identity stays acceptable by default.
        return True

    # ── Matching ───────────────────────────────────────────

    def best_match(self, options: list[str], default: str | None = None) -> str | None:
        """Return the option the client accepts with the highest q-value.

        Among candidates the client accepts (q>0), the one whose best matching
        range has the highest `(q, specificity)` wins, so a parameterized exact
        match beats a bare wildcard (RFC 9110 Sec. 12.5.1); ties go to the order
        in `options`. Returns `default` when no option has q>0, and `options[0]`
        for an empty header, since a missing Accept means "accept anything".

        Args:
            options: Candidates, in server preference order.
            default: Returned when the client accepts none of them.

        Returns:
            The winning option, or `default`.

        """
        if not self._options:
            return options[0] if options else default
        best_opt: str | None = default
        best_rank: tuple[float, int] = (0.0, -1)
        for opt in options:
            rank = self._match_rank(opt)
            if rank[0] > 0.0 and rank > best_rank:
                best_rank = rank
                best_opt = opt
        return best_opt

    def _match_rank(self, value: str) -> tuple[float, int]:
        """Return `(quality, specificity)` for `value` against this header.

        `specificity` is the score of the most specific matching client range
        (0 for non-MIME headers), which `best_match` uses to prefer a
        parameterized exact match over a wildcard at equal quality.

        Non-MIME headers rank through `quality_explicit`, not `quality`, because
        RFC 9110 Sec. 12.5.3 makes an explicit `q=0` a refusal a wildcard must
        not override. The MIME path resolves this by most-specific-range.
        """
        if not self._mime:
            return (self.quality_explicit(value), 0)
        return self._mime_best(_lookup_mime_key(value))

    def _mime_best(self, vkey: _MimeKey) -> tuple[float, int]:
        """Return `(quality, specificity)` for `vkey` across this header's ranges.

        The most specific matching client range wins regardless of q; among
        equally-specific ranges the higher q wins (RFC 9110 Sec. 12.5.1).
        """
        best_q = 0.0
        best_spec = -1
        for _opt, q, okey in self._options:
            if okey is None or not self._mime_matches(okey, vkey):
                continue
            spec = okey[3]
            if spec > best_spec or (spec == best_spec and q > best_q):
                best_spec = spec
                best_q = q
        return (best_q, best_spec)

    @staticmethod
    def _mime_matches(okey: _MimeKey, vkey: _MimeKey) -> bool:
        """Whether a client media range `okey` matches a server media type `vkey`.

        Type and subtype must match or be covered by the range's `*` wildcard,
        and every parameter the range names must be present with an equal value
        in the server type (RFC 9110 Sec. 12.5.1). An invalid range never matches.
        """
        otype, osub, oparams, ospec = okey
        if ospec < 0:
            return False
        vtype, vsub, vparams, _vspec = vkey
        if otype != "*" and otype != vtype:
            return False
        if osub != "*" and osub != vsub:
            return False
        # `<=`: the range's params must be a subset of the server type's.
        return oparams <= vparams

    def __contains__(self, value: str) -> bool:
        return self.quality(value) > 0

    def __bool__(self) -> bool:
        return any(q > 0 for _, q, _ in self._options)

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)


class Authorization:
    """Parsed `Authorization` header.

    Two common shapes are first-class:
    - `Basic` (RFC 7617): `.type == "basic"`, `.username` + `.password` set.
    - `Bearer` (RFC 6750): `.type == "bearer"`, `.token` set.

    Other schemes (Digest per RFC 7616, Negotiate, custom) populate `.params`
    with the comma-separated key="value" parameters parsed from the credentials
    portion; `.type` is the scheme name lower-cased.

    `Authorization.from_header(value)` returns `None` for empty or malformed
    input rather than raising.
    """

    __slots__ = ("type", "scheme", "raw", "username", "password", "token", "params")

    def __init__(
        self,
        type: str,
        raw: str,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        params: dict[str, str] | None = None,
    ) -> None:
        self.type = type
        # `scheme` is the original casing (e.g. "Bearer"); `type` is lowercase.
        self.scheme = raw.split(" ", 1)[0] if " " in raw else raw
        self.raw = raw
        self.username = username
        self.password = password
        self.token = token
        self.params = params or {}

    @classmethod
    def from_header(cls, header_value: str) -> Authorization | None:
        """Parse an `Authorization:` header value. Returns None on miss."""
        if not header_value:
            return None
        if " " not in header_value:
            # Single-token form (rare; some custom schemes use it).
            return cls(type=header_value.lower(), raw=header_value)
        scheme, _, credentials = header_value.partition(" ")
        scheme_lower = scheme.lower()

        if scheme_lower == "basic":
            # Shared with `HTTPBasic`, so the two cannot disagree. `None` covers
            # undecodable base64 and a colon-less value (RFC 7617 Sec. 2).
            decoded_pair = _decode_basic_credentials(credentials.strip())
            if decoded_pair is None:
                return cls(type="basic", raw=header_value)
            user, pw = decoded_pair
            return cls(type="basic", raw=header_value, username=user, password=pw)

        if scheme_lower == "bearer":
            return cls(type="bearer", raw=header_value, token=credentials.strip())

        # Digest / Negotiate / custom: key=value pairs, else a raw `token`.
        if "=" in credentials:
            _, params = parse_header_params(credentials, delimiter=",", unescape=True)
            return cls(type=scheme_lower, raw=header_value, params=params)
        return cls(type=scheme_lower, raw=header_value, token=credentials.strip())

    def __repr__(self) -> str:
        if self.type == "basic":
            return f"Authorization(type='basic', username={self.username!r})"
        if self.type == "bearer":
            return "Authorization(type='bearer')"
        return f"Authorization(type={self.type!r})"
