"""Unit tests for the shared header parameter walker."""

from __future__ import annotations

from veloce._header_parsing import parse_header_params

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
