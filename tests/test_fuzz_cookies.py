"""Property-based fuzz tests for the cookie parser — `http/cookies.py`.

Feeds arbitrary text at `parse_cookie` / `Cookies.from_cookie_header` and
asserts robustness invariants (no unhandled crash, well-formed output) plus
the `dump_cookie` -> `parse_cookie` round-trip for valid name/value pairs.
"""

from __future__ import annotations

import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from veloce.http.cookies import dump_cookie, parse_cookie
from veloce.http.datastructures import Cookies

pytestmark = pytest.mark.fuzz

# RFC 6265 cookie-name token characters (the set `dump_cookie` accepts).
_COOKIE_NAME_CHARS = "!#$%&'*+-.0123456789" + string.ascii_letters + "^_`|~"
_RESERVED = frozenset(
    {"expires", "max-age", "domain", "path", "secure", "httponly", "samesite", "partitioned"}
)


@given(st.text(max_size=200))
def test_parse_cookie_arbitrary_text_never_crashes(header: str) -> None:
    """Any string parses to a `{str: str}` dict — never raises."""
    result = parse_cookie(header)
    assert isinstance(result, dict)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in result.items())


@given(st.text(alphabet='=;, \t"%abcAB', max_size=120))
def test_parse_cookie_delimiter_soup_never_crashes(header: str) -> None:
    """Strings dense in cookie delimiters still parse cleanly."""
    assert isinstance(parse_cookie(header), dict)


@given(st.text(max_size=200))
def test_cookies_from_header_matches_parse_cookie(header: str) -> None:
    """`Cookies.from_cookie_header` agrees with `parse_cookie` (one code path)."""
    cookies = Cookies.from_cookie_header(header)
    assert isinstance(cookies, Cookies)
    # Both delegate to `iter_cookies`; first-wins de-duplication means the
    # dict view of each must carry the same first value for every name.
    parsed = parse_cookie(header)
    assert dict(cookies) == parsed


# The round-trip value space is the set of strings a cookie value can actually
# carry on the wire. Excluded by construction:
#   * CR / LF / NUL — `dump_cookie` rejects them (header-injection defense);
#   * lone surrogates — not UTF-8 encodable at all, so percent-quoting raises
#     `UnicodeEncodeError` (a property of the text, not a parser defect).
# A literal `%` now round-trips (`dump_cookie` encodes it as `%25`), so it is
# left in the alphabet rather than blacklisted.
_COOKIE_VALUE_TEXT = st.text(
    alphabet=st.characters(
        min_codepoint=1,
        blacklist_characters="\r\n",
        blacklist_categories=("Cs",),
    ),
    max_size=120,
)


@given(
    name=st.text(alphabet=_COOKIE_NAME_CHARS, min_size=1, max_size=40),
    value=_COOKIE_VALUE_TEXT,
)
def test_dump_then_parse_round_trips(name: str, value: str) -> None:
    """A valid name/value pair survives `dump_cookie` -> `parse_cookie`."""
    if name.lower() in _RESERVED:
        return
    header = dump_cookie(name, value, path=None)
    parsed = parse_cookie(header)
    assert parsed.get(name) == value


def test_literal_percent_value_round_trips() -> None:
    """A cookie value containing a literal percent sequence round-trips.

    `dump_cookie` encodes a literal `%` as `%25` (it is no longer in the quoting
    safe set), so `parse_cookie` restores the original value instead of decoding
    `%00` to a NUL byte.
    """
    header = dump_cookie("c", "%00", path=None)
    assert parse_cookie(header).get("c") == "%00"
    assert parse_cookie(dump_cookie("c", "100%", path=None)).get("c") == "100%"
