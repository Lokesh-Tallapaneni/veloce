"""HTTPDigest scheme — RFC 7616."""

from __future__ import annotations

import pytest

from veloce import HTTPDigest, HTTPDigestCredentials, HTTPException, Request


def _req(headers: dict | None = None) -> Request:
    return Request(method="GET", path="/x", query_string="", headers=headers or {}, body=b"")


def test_no_header_raises_with_digest_challenge():
    scheme = HTTPDigest(realm="testrealm@example.com")
    with pytest.raises(HTTPException) as exc:
        scheme(_req())
    assert exc.value.status_code == 401
    wwwa = exc.value.headers["WWW-Authenticate"]
    assert wwwa.startswith("Digest ")
    assert 'realm="testrealm%40example.com"' in wwwa  # URL-encoded
    assert "algorithm=SHA-256" in wwwa
    assert 'qop="auth"' in wwwa
    assert 'nonce="' in wwwa


def test_default_algorithm_is_sha256():
    """RFC 7616 §3.2 — SHA-256 is the preferred default; MD5 is back-compat only."""
    scheme = HTTPDigest(realm="r")
    assert scheme.algorithm == "SHA-256"


def test_explicit_md5_still_accepted_for_back_compat():
    scheme = HTTPDigest(realm="r", algorithm="MD5")
    assert scheme.algorithm == "MD5"
    with pytest.raises(HTTPException) as exc:
        scheme(_req())
    assert "algorithm=MD5" in exc.value.headers["WWW-Authenticate"]


def test_parses_backslash_then_quote_literal():
    """Literal `\\\"` in the wire (backslash, then escaped-quote) decodes to `\\\"`.

    The old chained .replace() would collapse `\\\\\"` to `\\\\` first and then
    miss the now-unescaped `\\\"`, corrupting the value.
    """
    # On the wire the field looks like:  field="a\\\"b"
    # i.e. a, literal-backslash (escaped as \\), literal-quote (escaped as \"), b
    header = 'Digest username="a\\\\\\"b", realm="r", response="x"'
    scheme = HTTPDigest(realm="r", auto_error=False)
    creds = scheme(_req({"authorization": header}))
    assert creds.username == 'a\\"b'


def test_parses_embedded_escaped_quote_and_backslash_separately():
    # username="he said \"hi\"" -> he said "hi"
    header = 'Digest username="he said \\"hi\\"", realm="r", response="x"'
    scheme = HTTPDigest(realm="r", auto_error=False)
    creds = scheme(_req({"authorization": header}))
    assert creds.username == 'he said "hi"'

    # realm="c:\\path" on the wire is realm="c:\\\\path" -> c:\path
    header2 = 'Digest username="u", realm="c:\\\\path", response="x"'
    creds2 = scheme(_req({"authorization": header2}))
    assert creds2.realm == "c:\\path"


def test_auto_error_false_returns_none_when_missing():
    scheme = HTTPDigest(realm="x", auto_error=False)
    assert scheme(_req()) is None


def test_parses_digest_response_fields():
    header = (
        'Digest username="alice", realm="testrealm@example.com", '
        'nonce="abc123", uri="/x", response="deadbeef", '
        'qop=auth, nc=00000001, cnonce="xyz", algorithm=MD5'
    )
    scheme = HTTPDigest(realm="testrealm@example.com")
    creds = scheme(_req({"authorization": header}))
    assert isinstance(creds, HTTPDigestCredentials)
    assert creds.username == "alice"
    assert creds.realm == "testrealm@example.com"
    assert creds.nonce == "abc123"
    assert creds.uri == "/x"
    assert creds.response == "deadbeef"
    assert creds.qop == "auth"
    assert creds.nc == "00000001"
    assert creds.cnonce == "xyz"
    assert creds.algorithm == "MD5"


def test_parses_with_escaped_quote_in_field():
    """Backslash-quote inside a quoted-string is unwrapped."""
    header = 'Digest username="al\\"ice", realm="r", response="x"'
    scheme = HTTPDigest(realm="r", auto_error=False)
    creds = scheme(_req({"authorization": header}))
    assert creds.username == 'al"ice'


def test_unknown_fields_ignored():
    """Extensions like `userhash=true` don't break parsing."""
    header = 'Digest username="u", realm="r", response="x", userhash=true'
    scheme = HTTPDigest(realm="r", auto_error=False)
    creds = scheme(_req({"authorization": header}))
    assert creds.username == "u"


def test_custom_nonce_factory():
    scheme = HTTPDigest(realm="r", nonce_factory=lambda: "FIXED")
    with pytest.raises(HTTPException) as exc:
        scheme(_req())
    assert 'nonce="FIXED"' in exc.value.headers["WWW-Authenticate"]
