"""`request.authorization` and `HTTPBasic` agree on every Basic payload.

RFC 7617 credentials were decoded in two places - `Authorization.from_header`
and `HTTPBasic.__call__` - and the copies disagreed. Both are answers to the same
question ("what credentials does this header carry?"), asked by application code
and by the security scheme, so a disagreement means `request.authorization`
reports a login the scheme refuses with a `401`.

Two divergences were found, and the second only by testing the two against each
other rather than separately:

* a **colon-less** payload. RFC 7617 Sec. 2 makes the colon mandatory, so
  `dXNlcm9ubHk=` is not "a username with an empty password" - it is not
  credentials. (Fixed earlier in this review.)
* **surrounding whitespace**. `from_header` trimmed the payload and `HTTPBasic`
  did not, so `Basic  dTpw` (two spaces, which RFC 9110 Sec. 11.6.1 permits)
  yielded credentials from one and a `401` from the other.

Both now decode through one `_decode_basic_credentials`, which returns `None` for
every malformed shape. These tests assert the two **against each other** across
the payload space; asserting each separately is what allowed the drift.
"""

from __future__ import annotations

import base64

import pytest

from veloce import Depends, Veloce
from veloce.exceptions import HTTPException
from veloce.http.datastructures import Authorization
from veloce.security.http import HTTPBasic
from veloce.testclient import TestClient


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


VALID = {
    "normal": (_b64(b"user:pass"), ("user", "pass")),
    "empty-password": (_b64(b"user:"), ("user", "")),
    "empty-userid": (_b64(b":pass"), ("", "pass")),
    "colon-in-password": (_b64(b"user:pa:ss"), ("user", "pa:ss")),
    "colon-only": (_b64(b":"), ("", "")),
    "leading-space": (" " + _b64(b"u:p"), ("u", "p")),
    "surrounding-space": (" " + _b64(b"u:p") + " ", ("u", "p")),
    "unicode": (_b64("café:pässwörd".encode()), ("café", "pässwörd")),
}

MALFORMED = {
    "no-colon": _b64(b"useronly"),
    "empty": _b64(b""),
    "non-utf8": base64.b64encode(bytes([0xFF, 0xFE])).decode(),
    "not-base64": "!!!notb64!!!",
    "bad-padding": _b64(b"a:b") + "=",
}


def _from_header(payload: str):
    parsed = Authorization.from_header(f"Basic {payload}")
    if parsed is None:
        return None
    if parsed.username is None:
        return None
    return (parsed.username, parsed.password)


def _scheme(payload: str):
    from tests.conftest import make_request

    request = make_request(path="/", headers={"Authorization": f"Basic {payload}"})
    try:
        credentials = HTTPBasic(auto_error=False)(request)
    except HTTPException:
        return None
    if credentials is None:
        return None
    return (credentials.username, credentials.password)


# ── valid payloads decode identically ────────────────────────────────


@pytest.mark.parametrize("name", list(VALID))
def test_both_readers_decode_a_valid_payload_the_same(name):
    payload, expected = VALID[name]
    assert _from_header(payload) == expected
    assert _scheme(payload) == expected


@pytest.mark.parametrize("name", list(VALID))
def test_the_two_readers_agree_on_a_valid_payload(name):
    """Stated as the property, not as two separate expectations."""
    payload, _expected = VALID[name]
    assert _from_header(payload) == _scheme(payload)


def test_whitespace_does_not_change_the_credentials():
    """The second divergence: only one reader trimmed."""
    plain = _b64(b"u:p")
    assert _from_header(plain) == _from_header(" " + plain + " ")
    assert _scheme(plain) == _scheme(" " + plain + " ")


# ── malformed payloads are refused by both ───────────────────────────


@pytest.mark.parametrize("name", list(MALFORMED))
def test_both_readers_refuse_a_malformed_payload(name):
    payload = MALFORMED[name]
    assert _from_header(payload) is None
    assert _scheme(payload) is None


def test_a_colon_less_payload_is_not_an_empty_password_login():
    """The first divergence, stated as the security property it is."""
    assert _from_header(_b64(b"useronly")) is None
    assert _scheme(_b64(b"useronly")) is None


@pytest.mark.parametrize("name", list(VALID) + list(MALFORMED))
def test_the_two_readers_never_disagree(name):
    """The property the shared decoder exists to guarantee."""
    payload = VALID[name][0] if name in VALID else MALFORMED[name]
    assert _from_header(payload) == _scheme(payload), payload


# ── and a valid login still authenticates end to end ─────────────────


def test_a_valid_credential_still_authenticates():
    """The negative: refusing everything would satisfy the agreement tests."""

    app = Veloce(openapi_url=None)
    scheme = HTTPBasic()

    @app.get("/me")
    async def me(credentials=Depends(scheme)):
        return {"user": credentials.username}

    client = TestClient(app)
    header = "Basic " + _b64(b"ada:secret")
    assert client.get("/me", headers={"Authorization": header}).json() == {"user": "ada"}
    assert client.get("/me").status_code == 401
    assert (
        client.get("/me", headers={"Authorization": "Basic " + _b64(b"nocolon")}).status_code == 401
    )
