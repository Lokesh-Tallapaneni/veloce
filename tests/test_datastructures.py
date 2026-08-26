"""Tests for the public types in `veloce.http.datastructures`.

Focused on shapes that did not have a dedicated test file before — the
`Authorization` parser in particular, which is exercised via the request
pipeline elsewhere but never tested at the unit level.
"""

from __future__ import annotations

import base64

from veloce import URL, FormData, Headers, HTTPBasic, Security, Veloce
from veloce.http.datastructures import Authorization
from veloce.security import HTTPBasicCredentials
from veloce.testclient import TestClient


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


# ── RFC 7617 Sec. 2: the colon in `userid ":" password` is mandatory ──
#
# `Authorization.from_header` reported `username='nocolon', password=''` for a
# colon-less payload while `HTTPBasic` - reading the same header - refused it
# with a 401. User code that reads `request.authorization` directly therefore
# saw a username for a header the auth scheme itself rejects, which is
# auth-bypass-shaped. A malformed payload now parses like malformed base64
# already did: the scheme is reported, the credentials are not.


def test_colon_less_basic_credentials_yield_no_username():
    # `bm9jb2xvbg==` decodes to `nocolon`.
    auth = Authorization.from_header("Basic bm9jb2xvbg==")
    assert auth is not None
    assert auth.type == "basic"
    assert auth.username is None
    assert auth.password is None


def test_empty_basic_credentials_yield_no_username():
    auth = Authorization.from_header("Basic ")
    assert auth is not None
    assert auth.username is None


def test_an_empty_password_is_still_valid():
    """`user:` is well-formed - the colon is there and the password is empty."""
    auth = Authorization.from_header("Basic dXNlcjo=")
    assert auth is not None
    assert auth.username == "user"
    assert auth.password == ""


def test_an_empty_username_is_still_valid():
    """`:pass` is well-formed too; RFC 7617 does not require a non-empty userid."""
    auth = Authorization.from_header("Basic OnBhc3M=")
    assert auth is not None
    assert auth.username == ""
    assert auth.password == "pass"


def test_a_password_may_contain_colons():
    """Only the first colon separates - `user:pa:ss` is one password."""
    auth = Authorization.from_header("Basic dXNlcjpwYTpzcw==")
    assert auth is not None
    assert auth.username == "user"
    assert auth.password == "pa:ss"


def test_the_two_basic_parsers_agree_on_what_authenticates():
    """The property the finding is about: one header, one answer.

    `HTTPBasic` was already right, so the datastructure is asserted against it
    rather than against a fixed expectation and the two cannot drift again.

    A *malformed* header is a 401 from the scheme whatever `auto_error` says -
    that is deliberate, and separate from the absent-header case `auto_error`
    governs. The property asserted is that the datastructure reports a username
    exactly when the scheme accepts one.
    """
    app = Veloce(openapi_url=None)
    scheme = HTTPBasic(auto_error=False)

    @app.get("/who")
    async def who(cred: HTTPBasicCredentials | None = Security(scheme)):
        return {"user": None if cred is None else cred.username}

    client = TestClient(app)
    for payload in ("nocolon", "user:pass", ":pass", "user:", "", "a:b:c"):
        header = "Basic " + base64.b64encode(payload.encode()).decode()
        resp = client.get("/who", headers={"Authorization": header})
        accepted = resp.json().get("user") if resp.status_code == 200 else None
        parsed = Authorization.from_header(header)
        assert parsed is not None
        assert parsed.username == accepted, payload


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


class TestDataStructures:
    def test_url_replace(self):
        url = URL(scheme="http", host="example.com", path="/api")
        new_url = url.replace(scheme="https")
        assert new_url.scheme == "https"
        assert new_url.host == "example.com"

    def test_url_netloc_default_port(self):
        url = URL(host="example.com", port=80)
        assert url.netloc == "example.com"

    def test_url_netloc_custom_port(self):
        url = URL(host="example.com", port=9000)
        assert url.netloc == "example.com:9000"

    def test_headers_case_insensitive(self):
        h = Headers({"Content-Type": "application/json"})
        assert h.get("content-type") == "application/json"
        assert h.get("CONTENT-TYPE") == "application/json"

    def test_formdata_getlist(self):
        # FormData is a MultiDict — repeated keys are stored as separate
        # entries, not a list-as-value. Construction from a list of tuples
        # is the multi-value idiom.
        fd = FormData([("items", "a"), ("items", "b"), ("items", "c")])
        assert fd.getlist("items") == ["a", "b", "c"]
        assert fd.getlist("missing") == []
