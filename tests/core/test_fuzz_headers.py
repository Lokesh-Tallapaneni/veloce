"""Property-based fuzz tests for the header / query parsers.

Targets the parsers in `http/datastructures.py`: `QueryParams.from_query_string`,
`AcceptHeader.parse`, `Authorization.from_header`, `RangeSpec.parse`, and the
Host-header splitter `_parse_host_header`. Each is fed arbitrary text and must
either return structurally valid output or raise only its declared exception —
never an unhandled error or a hang.
"""

from __future__ import annotations

import contextlib
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from veloce.exceptions import RequestURITooLong
from veloce.http.datastructures import (
    _DEFAULT_HOST,
    AcceptHeader,
    Authorization,
    QueryParams,
    RangeSpec,
    _parse_host_header,
)

pytestmark = pytest.mark.fuzz


# ── Query string ──────────────────────────────────────────────────────


@given(st.text(max_size=200))
def test_query_parser_arbitrary_text_never_crashes(query: str) -> None:
    """Arbitrary text yields `QueryParams` or the declared 414 — nothing else."""
    try:
        params = QueryParams.from_query_string(query)
    except RequestURITooLong:
        return  # declared field-count cap rejection
    assert isinstance(params, QueryParams)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in params.items())


@given(st.text(alphabet="=&;%+ ab12", max_size=120))
def test_query_parser_delimiter_soup_never_crashes(query: str) -> None:
    """Strings dense in query delimiters parse without raising."""
    with contextlib.suppress(RequestURITooLong):
        QueryParams.from_query_string(query)


# ── Accept-* headers ──────────────────────────────────────────────────


@given(raw=st.text(max_size=200), mime=st.booleans())
def test_accept_parse_never_crashes(raw: str, mime: bool) -> None:
    """Any string parses; q-values stay finite and well-formed."""
    header = AcceptHeader.parse(raw, mime=mime)
    assert isinstance(header.values, list)
    assert all(isinstance(v, str) for v in header.values)
    for value in header.values:
        q = header.quality(value)
        assert isinstance(q, float)
        assert math.isfinite(q)  # a poisoned q-value (NaN / inf) would be a bug


@given(
    raw=st.text(max_size=120),
    options=st.lists(st.text(min_size=1, max_size=20), max_size=6),
)
def test_accept_best_match_returns_an_option_or_default(raw: str, options: list[str]) -> None:
    """`best_match` returns one of the offered options, the default, or None."""
    header = AcceptHeader.parse(raw, mime=True)
    chosen = header.best_match(options, default="__sentinel__")
    assert chosen is None or chosen == "__sentinel__" or chosen in options


# ── Authorization ─────────────────────────────────────────────────────


@given(st.text(max_size=200))
def test_authorization_parse_never_crashes(raw: str) -> None:
    """Any value returns an `Authorization` or `None` — never raises."""
    auth = Authorization.from_header(raw)
    assert auth is None or isinstance(auth, Authorization)
    if auth is not None:
        assert isinstance(auth.type, str)
        assert isinstance(auth.params, dict)


@given(st.text(alphabet='Basic Bearer Digest =," abcXYZ123/+', max_size=120))
def test_authorization_scheme_soup_never_crashes(raw: str) -> None:
    """Scheme-keyword-dense strings parse without raising."""
    Authorization.from_header(raw)


# ── Range ─────────────────────────────────────────────────────────────


@given(st.text(max_size=120))
def test_range_parse_never_crashes(raw: str) -> None:
    """Any value returns a `RangeSpec` or `None`, with well-typed ranges."""
    spec = RangeSpec.parse(raw)
    assert spec is None or isinstance(spec, RangeSpec)
    if spec is not None:
        assert spec.ranges  # a returned spec always carries at least one range
        for start, end in spec.ranges:
            assert start is None or isinstance(start, int)
            assert end is None or isinstance(end, int)


@given(st.text(alphabet="bytes=-,0129 ", max_size=60))
def test_range_byte_spec_soup_never_crashes(raw: str) -> None:
    """Byte-range-shaped strings parse without raising."""
    RangeSpec.parse(raw)


# ── Host header ───────────────────────────────────────────────────────


@given(st.text(max_size=120))
def test_parse_host_header_never_crashes(raw: str) -> None:
    """Any Host value returns a validated `(host, port)` — never raises.

    A malformed Host must degrade to the safe default rather than leaking the
    attacker-controlled string or a bad port (the invariant the validator
    exists to enforce).
    """
    host, port = _parse_host_header(raw)
    assert isinstance(host, str)
    assert port is None or (isinstance(port, int) and 1 <= port <= 65535)
    if host == _DEFAULT_HOST:
        return
    # An accepted host must not carry path / query / CRLF injection payloads.
    assert not any(c in host for c in "/?#\r\n ")


@given(st.text(alphabet="[]:.0123456789abcdef", max_size=50))
def test_parse_host_header_ipv6_soup_never_crashes(raw: str) -> None:
    """IPv6-shaped strings (brackets, colons, hex) parse without raising."""
    host, port = _parse_host_header(raw)
    assert isinstance(host, str)
    assert port is None or 1 <= port <= 65535
