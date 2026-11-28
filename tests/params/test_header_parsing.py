"""Unit tests for the shared header parameter walker."""

from __future__ import annotations

import pytest

from veloce import Request, Response
from veloce._header_parsing import (
    parse_header_params,
    parse_media_type_params,
    split_outside_quotes,
    split_outside_quotes_checked,
)
from veloce.http.datastructures import AcceptHeader

# --- Content-Disposition shape (`;` delimiter, unescape on) -----------------


def test_content_disposition_type_and_params():
    prefix, params = parse_header_params(
        'form-data; name="upload"; filename="report.pdf"',
        delimiter=";",
        unescape=True,
    )
    assert prefix == "form-data"
    assert params == {"name": "upload", "filename": "report.pdf"}


def test_quoted_delimiter_preserved_in_value():
    _, params = parse_header_params('form-data; name="a;b"', delimiter=";", unescape=True)
    assert params["name"] == "a;b"


def test_backslash_escape_unescape_true():
    _, params = parse_header_params(r'form-data; name="a\"b"', delimiter=";", unescape=True)
    assert params["name"] == 'a"b'


def test_double_backslash_unescape_true():
    _, params = parse_header_params(r'form-data; name="a\\b"', delimiter=";", unescape=True)
    assert params["name"] == "a\\b"


def test_keys_are_lowercased():
    _, params = parse_header_params(
        'form-data; Name="x"; FileName="y"', delimiter=";", unescape=True
    )
    assert params == {"name": "x", "filename": "y"}


# --- Digest shape (`,` delimiter, unescape on) ------------------------------


def test_digest_pairs_no_prefix():
    prefix, params = parse_header_params(
        'username="alice", realm="r", nc=00000001', delimiter=",", unescape=True
    )
    assert prefix == ""
    assert params == {"username": "alice", "realm": "r", "nc": "00000001"}


def test_digest_unquoted_value_is_stripped():
    _, params = parse_header_params("qop=auth , nc=00000001", delimiter=",", unescape=True)
    assert params["qop"] == "auth"
    assert params["nc"] == "00000001"


def test_digest_quoted_value_keeps_inner_whitespace():
    _, params = parse_header_params('realm=" testrealm "', delimiter=",", unescape=True)
    assert params["realm"] == " testrealm "


def test_digest_comma_inside_quoted_value():
    _, params = parse_header_params('realm="a,b", qop=auth', delimiter=",", unescape=True)
    assert params == {"realm": "a,b", "qop": "auth"}


def test_digest_escaped_quote_then_backslash_round_trips():
    # On the wire: a\\"b   ->   a"b (with leading backslash preserved? no - both eat)
    # `\\` -> `\`, `\"` -> `"`. So `a\\\"b` (5 chars) decodes to `a\"b` (3 chars
    # after both escapes resolved... actually 4: a, \, ", b).
    _, params = parse_header_params(r'username="a\\\"b"', delimiter=",", unescape=True)
    assert params["username"] == 'a\\"b'


# --- Authorization fallback shape (`,` delimiter, unescape off) -------------


def test_authz_unescape_false_keeps_backslash_literal():
    # Walker still treats `\"` as not-a-quote-terminator (boundary detection
    # uses the escape even when unescape=False), so the value reads through
    # to the real close quote. Both the backslash and the escaped char are
    # emitted verbatim.
    _, params = parse_header_params(r'name="a\"b"', delimiter=",", unescape=False)
    assert params["name"] == 'a\\"b'


def test_authz_unescape_true_now_decodes_escape():
    # The opt-in fix for the original `_split_authz_params` bug.
    _, params = parse_header_params(r'name="a\"b"', delimiter=",", unescape=True)
    assert params["name"] == 'a"b'


# --- Edge cases -------------------------------------------------------------


def test_empty_value_returns_empty_prefix_and_no_params():
    prefix, params = parse_header_params("", delimiter=";", unescape=True)
    assert prefix == ""
    assert params == {}


def test_tokens_without_equals_after_first_are_dropped():
    prefix, params = parse_header_params("form-data; orphan; name=x", delimiter=";", unescape=True)
    assert prefix == "form-data"
    assert params == {"name": "x"}


def test_first_token_is_prefix_only_when_no_equals():
    # All tokens are key=value -> prefix is empty, every pair lands in params.
    prefix, params = parse_header_params("a=1; b=2", delimiter=";", unescape=True)
    assert prefix == ""
    assert params == {"a": "1", "b": "2"}


def test_trailing_backslash_does_not_index_past_end():
    # `\` at the final position of a quoted string has no next char to escape;
    # the walker must not crash.
    _, params = parse_header_params('name="x\\', delimiter=";", unescape=True)
    assert "name" in params


def test_empty_key_dropped():
    _, params = parse_header_params("=value; name=ok", delimiter=";", unescape=True)
    assert params == {"name": "ok"}


# --- split_outside_quotes ---------------------------------------------------


def test_split_outside_quotes_plain():
    assert split_outside_quotes("a,b", ",") == ["a", "b"]


def test_split_outside_quotes_comma_in_quotes_not_split():
    assert split_outside_quotes('host="a,b"', ",") == ['host="a,b"']


def test_split_outside_quotes_mixed():
    assert split_outside_quotes('for=x; host="a,b"', ",") == ['for=x; host="a,b"']


def test_split_outside_quotes_escaped_quote():
    assert split_outside_quotes(r'k="a\",b"', ",") == [r'k="a\",b"']


def test_split_outside_quotes_empty_and_trailing():
    assert split_outside_quotes("", ",") == [""]
    assert split_outside_quotes("a,", ",") == ["a", ""]


# --- Media-type parameters (`parse_media_type_params`) ----------------------
#
# The media-type parameter list is read through the same walker, because a
# parameter value may be a quoted-string and a quoted-string may contain the
# `;` that otherwise separates parameters (RFC 9110 Sec. 5.6.4-5.6.6). A plain
# `split(";")` cut such a value short and left the opening quote on what
# survived, so the `Content-Type` accessors and the `Accept` media-range key
# disagreed with `parse_header_params` - the walker every other header parser
# here reads a quoted value with - about the same header.
#
# Note `boundary` itself cannot legally contain `;` (RFC 2046 Sec. 5.1.1
# excludes it from the boundary charset, and the multipart parser rejects it),
# so the examples below use parameters that can.


def _params(rest: str) -> dict[str, str]:
    return dict(parse_media_type_params(rest))


def _walker(rest: str) -> dict[str, str]:
    _, params = parse_header_params(rest, delimiter=";", unescape=True)
    return params


@pytest.mark.parametrize(
    ("rest", "expected"),
    [
        (' boundary="a;b"', {"boundary": "a;b"}),
        (' note="has;semi"', {"note": "has;semi"}),
        (' name="f.txt"; filename="a;b.txt"', {"name": "f.txt", "filename": "a;b.txt"}),
        (
            ' boundary="--x;y--"; charset=utf-8',
            {"boundary": "--x;y--", "charset": "utf-8"},
        ),
        (' a="1"; b="2;3"', {"a": "1", "b": "2;3"}),
    ],
)
def test_a_semicolon_inside_a_quoted_value_does_not_end_the_parameter(rest, expected):
    assert _params(rest) == expected


@pytest.mark.parametrize(
    ("rest", "expected"),
    [
        (r' boundary="a\"b"', {"boundary": 'a"b'}),
        (r' boundary="a\;b"', {"boundary": "a;b"}),
    ],
)
def test_an_escape_inside_a_quoted_value_is_decoded(rest, expected):
    assert _params(rest) == expected


#: Every ordinary shape, none of which carries a quote, so all take the guarded
#: split. They must land exactly where the walker would.
_UNQUOTED = [
    "",
    " ",
    ";",
    ";;",
    "charset=utf-8",
    " charset=utf-8",
    "  charset = utf-8  ",
    " CHARSET=UTF-8",
    " charset=utf-8; boundary=xyz",
    " a=1;;b=2",
    " a=b=c",
    " a=",
    " =b",
    " novalue",
    " novalue; charset=utf-8",
    " boundary=a b",
    "\ta=1",
]


@pytest.mark.parametrize("rest", _UNQUOTED)
def test_the_guarded_split_lands_where_the_walker_would(rest):
    """The guard's claim: with no quote present the two cannot disagree."""
    assert _params(rest) == _walker(rest)


@pytest.mark.parametrize(
    ("rest", "expected"),
    [
        (" charset=utf-8", {"charset": "utf-8"}),
        (" CHARSET=UTF-8", {"charset": "UTF-8"}),
        ('  charset = "utf-8" ', {"charset": "utf-8"}),
        (" charset=utf-8; boundary=xyz", {"charset": "utf-8", "boundary": "xyz"}),
        (' boundary=" padded "', {"boundary": " padded "}),
        (' boundary=""', {"boundary": ""}),
        (" a=b=c", {"a": "b=c"}),
        ("", {}),
        (" novalue", {}),
        (" =b", {}),
    ],
)
def test_the_ordinary_shapes_are_unchanged(rest, expected):
    """Keys lowercase, values keep their case, a nameless parameter is dropped."""
    assert _params(rest) == expected


def test_a_later_parameter_of_the_same_name_wins():
    assert _params(' boundary="a"; boundary="b"') == {"boundary": "b"}


# --- The accessors that read it ---------------------------------------------


def test_a_request_and_a_response_read_a_quoted_parameter_the_same_way():
    """Three readers of one header: they must not give three answers."""
    content_type = 'text/plain; profile="a;b"'
    request = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": content_type},
        body=b"",
    )
    _, walker_params = parse_header_params(content_type, delimiter=";", unescape=True)
    assert request.mimetype_params == {"profile": "a;b"}
    assert Response(body=b"", content_type=content_type).mimetype_params == {"profile": "a;b"}
    assert walker_params == {"profile": "a;b"}


def test_the_mimetype_itself_is_unaffected_by_a_quoted_parameter():
    request = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": 'text/plain; profile="a;b"'},
        body=b"",
    )
    assert request.mimetype == "text/plain"


def test_an_accept_media_range_keeps_a_quoted_parameter_whole():
    """The third reader: the `Accept` media-range key is built from these too.

    A parameterised media range matches an offer carrying the same parameters,
    so a `;` swallowed inside a quoted value would key the range on `profile`
    values that were never sent.
    """
    accept = AcceptHeader.parse('text/html; profile="a;b"', mime=True)
    assert accept.best_match(['text/html; profile="a;b"']) == 'text/html; profile="a;b"'
    assert accept.best_match(['text/html; profile="a;c"']) is None
    assert accept.best_match(['text/html; profile="a']) is None


# --- Whitespace around a quoted region --------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('form-data; filename="r.pdf"', "r.pdf"),
        ('form-data; filename ="r.pdf"', "r.pdf"),
        ('form-data; filename= "r.pdf"', "r.pdf"),
        ('form-data; filename = "r.pdf" ', "r.pdf"),
        ('form-data; filename="  spaced  "', "  spaced  "),
        ('form-data; filename = "  spaced  " ', "  spaced  "),
    ],
)
def test_whitespace_outside_a_quoted_value_is_trimmed_from_both_sides(value, expected):
    """Only what was quoted is the value; padding around it never was.

    The trailing side was already trimmed. The leading side was not, so a
    `filename = "r.pdf"` yielded a value with a space welded to the front.
    """
    _, params = parse_header_params(value, delimiter=";", unescape=True)
    assert params["filename"] == expected


@pytest.mark.parametrize(
    ("rest", "expected"),
    [
        (' charset = "utf-8"', "utf-8"),
        ('  charset = "utf-8" ', "utf-8"),
        (' charset="utf-8"', "utf-8"),
        (" charset = utf-8", "utf-8"),
        (' charset=" padded "', " padded "),
    ],
)
def test_a_media_type_parameter_trims_the_same_way(rest, expected):
    assert dict(parse_media_type_params(rest))["charset"] == expected


def test_a_balanced_header_reports_no_unterminated_quote():
    """POSITIVE: the ordinary case must not be flagged."""
    parts, unterminated = split_outside_quotes_checked('a=1, b="x,y", c=3', ",")
    assert parts == ["a=1", ' b="x,y"', " c=3"]
    assert unterminated is False


def test_an_unterminated_quote_is_reported():
    """NEGATIVE: the state that lets a comma swallow every later element."""
    parts, unterminated = split_outside_quotes_checked('a=", b=2, c=3', ",")
    assert unterminated is True
    assert len(parts) == 1


def test_the_plain_splitter_still_returns_only_the_parts():
    """POSITIVE: the shared caller's contract must not move."""
    value = 'a=1, b="x,y"'
    assert split_outside_quotes(value, ",") == split_outside_quotes_checked(value, ",")[0]
