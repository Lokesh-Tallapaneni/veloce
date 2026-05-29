"""Tests for the public types in `veloce.http.datastructures`.

Focused on shapes that did not have a dedicated test file before — the
`Authorization` parser in particular, which is exercised via the request
pipeline elsewhere but never tested at the unit level.
"""

from __future__ import annotations

from veloce.http.datastructures import Authorization


def test_basic_auth_parses_username_and_password():
    # `dXNlcjpwYXNz` decodes to `user:pass`.
    auth = Authorization.from_header("Basic dXNlcjpwYXNz")
    assert auth is not None
    assert auth.type == "basic"
    assert auth.username == "user"
    assert auth.password == "pass"
    assert auth.scheme == "Basic"


def test_basic_auth_malformed_base64_still_returns_object():
    auth = Authorization.from_header("Basic !!!not-base64!!!")
    assert auth is not None
    assert auth.type == "basic"
    assert auth.username is None


def test_bearer_extracts_token():
    auth = Authorization.from_header("Bearer abc.def.ghi")
    assert auth is not None
    assert auth.type == "bearer"
    assert auth.token == "abc.def.ghi"


def test_digest_params_collected_into_dict():
    auth = Authorization.from_header(
        'Digest username="alice", realm="r", nonce="n", uri="/x", '
        'response="deadbeef", qop=auth, nc=00000001, cnonce="xyz"'
    )
    assert auth is not None
    assert auth.type == "digest"
    assert auth.params["username"] == "alice"
    assert auth.params["realm"] == "r"
    assert auth.params["qop"] == "auth"
    assert auth.params["nc"] == "00000001"


def test_digest_comma_inside_quoted_value_not_a_split():
    auth = Authorization.from_header('Digest realm="a,b", qop=auth')
    assert auth is not None
    assert auth.params["realm"] == "a,b"
    assert auth.params["qop"] == "auth"


def test_digest_backslash_escape_inside_quoted_value():
    """Regression for the old `_split_authz_params` bug.

    `name="a\\\"b"` on the wire (backslash, then escaped quote) used to
    produce `a\\"b` because the old splitter retained quotes literally
    and the caller only `.strip('"')`'d, leaving the backslash + quote
    intact. The consolidated walker decodes `\\\"` to `"` and `\\\\` to `\\`.
    """
    auth = Authorization.from_header(r'Digest name="a\"b"')
    assert auth is not None
    assert auth.params["name"] == 'a"b'


def test_digest_double_backslash_in_value():
    auth = Authorization.from_header(r'Digest path="c:\\dir"')
    assert auth is not None
    assert auth.params["path"] == "c:\\dir"


def test_scheme_with_single_token_credentials_falls_back_to_token():
    auth = Authorization.from_header("Negotiate opaqueblob")
    assert auth is not None
    assert auth.type == "negotiate"
    assert auth.token == "opaqueblob"


def test_empty_header_returns_none():
    assert Authorization.from_header("") is None


def test_single_token_no_space_treated_as_type():
    auth = Authorization.from_header("solo")
    assert auth is not None
    assert auth.type == "solo"
