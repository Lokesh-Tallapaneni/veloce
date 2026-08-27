"""A non-ASCII credential is rejected, not a crash.

One bug class at three sites. In each, a value the caller fully controls reaches
a comparison or an `ascii` encode that raises on non-ASCII, and nothing catches
it - so the answer to "are you authenticated?" is a `500` rather than a `401` or
`403`. All three are reachable *before* authentication, so no credential is
needed to trigger them.

Measured on a JWT-guarded route before the fix:

    valid        -> 200
    garbage      -> 401
    non-ASCII    -> 500     UnicodeEncodeError at security/jwt.py:188

The three sites and what each raised:

| Site | Input | Raised |
|---|---|---|
| `decode_jwt` | non-ASCII byte in a token segment | `UnicodeEncodeError` |
| `CSRFMiddleware._matches` | non-ASCII `X-CSRF-Token` | `TypeError` from `compare_digest` |
| `_verify_pkce` | non-ASCII `code_verifier` | `UnicodeEncodeError` |

`_b64decode` runs in binascii's relaxed mode and silently drops characters
outside the alphabet, which is why a token carrying a stray non-ASCII byte still
parses a valid header and reaches the encode at all.

Each fix follows a pattern already in the tree rather than inventing one:
`signing.py:161` already converts exactly this `UnicodeEncodeError` into its
domain error, and `veloce.safe.constant_time_compare` already exists for the
"operands may be `str`" case, UTF-8 encoding before comparing.

The tests assert the *status code*, not the exception type, because the contract
that matters to a caller is "an unparseable credential is refused" - and an
`UnicodeEncodeError` is not a `JWTError`, so an auth dependency written the
documented way (`except JWTError: raise Unauthorized`) never caught it.
"""

from __future__ import annotations

import pytest

from veloce import CSRFMiddleware, Depends, HTTPBearer, Unauthorized, Veloce
from veloce._internal import _b64encode
from veloce.contrib.mcp.authorization import _verify_pkce
from veloce.safe import constant_time_compare
from veloce.security.jwt import JWTError, decode_jwt, encode_jwt
from veloce.testclient import TestClient

# A character that is valid UTF-8 and outside ASCII. Chosen over an emoji so the
# failure is about the codec rather than about surrogate pairs.
NON_ASCII = "é"


def _jwt_app() -> tuple[Veloce, str]:
    """A route guarded exactly the way the documentation writes it."""
    app = Veloce(openapi_url=None)
    bearer = HTTPBearer()

    async def current_user(token: str = Depends(bearer)) -> dict:
        try:
            return decode_jwt(token, "secret", algorithms=["HS256"])
        except JWTError as err:
            raise Unauthorized("bad token") from err

    @app.get("/me")
    async def me(user: dict = Depends(current_user)) -> dict:
        return {"sub": user["sub"]}

    return app, encode_jwt({"sub": "ada"}, "secret")


# ── F1a: decode_jwt ──────────────────────────────────────────────────


def test_a_non_ascii_token_segment_raises_a_jwt_error():
    """The defect: `UnicodeEncodeError` escaped, so `except JWTError` missed it."""
    token = encode_jwt({"sub": "x"}, "secret")
    header, payload, signature = token.split(".")
    with pytest.raises(JWTError):
        decode_jwt(f"{header}.{NON_ASCII}{payload}.{signature}", "secret", algorithms=["HS256"])


@pytest.mark.parametrize("position", ["header", "payload", "signature"])
def test_a_non_ascii_byte_in_any_segment_raises_a_jwt_error(position: str):
    token = encode_jwt({"sub": "x"}, "secret")
    parts = token.split(".")
    index = {"header": 0, "payload": 1, "signature": 2}[position]
    parts[index] = NON_ASCII + parts[index]
    with pytest.raises(JWTError):
        decode_jwt(".".join(parts), "secret", algorithms=["HS256"])


def test_a_guarded_route_answers_401_for_a_non_ascii_token():
    """End to end, and the reason this is HIGH: it is a pre-auth crash."""
    app, token = _jwt_app()
    header, payload, signature = token.split(".")
    response = TestClient(app).get(
        "/me", headers={"Authorization": f"Bearer {header}.{NON_ASCII}{payload}.{signature}"}
    )
    assert response.status_code == 401


def test_a_guarded_route_still_accepts_a_valid_token():
    """The negative direction: the fix must not refuse a good credential."""
    app, token = _jwt_app()
    response = TestClient(app).get("/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"sub": "ada"}


def test_a_guarded_route_still_answers_401_for_garbage():
    app, token = _jwt_app()
    header, _payload, signature = token.split(".")
    response = TestClient(app).get(
        "/me", headers={"Authorization": f"Bearer {header}.AAAA.{signature}"}
    )
    assert response.status_code == 401


def test_the_jwt_error_names_the_cause():
    token = encode_jwt({"sub": "x"}, "secret")
    header, payload, signature = token.split(".")
    with pytest.raises(JWTError, match="ASCII"):
        decode_jwt(f"{header}.{NON_ASCII}{payload}.{signature}", "secret", algorithms=["HS256"])


# ── F1b: CSRF token comparison ───────────────────────────────────────


def _csrf_app() -> Veloce:

    app = Veloce(openapi_url=None)
    app.add_middleware(CSRFMiddleware(token_factory=lambda: "DETERMINISTIC"))

    @app.get("/form")
    async def form() -> dict:
        return {"ok": True}

    @app.post("/submit")
    async def submit() -> dict:
        return {"ok": True}

    return app


def test_a_non_ascii_csrf_header_is_refused_not_a_crash():
    """The defect: `compare_digest` raises `TypeError` on a non-ASCII `str`."""
    client = TestClient(_csrf_app())
    client.get("/form")  # obtain the cookie
    response = client.post("/submit", headers={"X-CSRF-Token": f"tok{NON_ASCII}en"})
    assert response.status_code == 403


def test_a_non_ascii_csrf_form_field_is_refused():
    client = TestClient(_csrf_app())
    client.get("/form")
    response = client.post("/submit", data={"csrf_token": f"tok{NON_ASCII}en"})
    assert response.status_code == 403


def test_a_missing_csrf_token_is_still_refused():
    """The negative: the pre-existing refusal must not have changed."""
    client = TestClient(_csrf_app())
    client.get("/form")
    assert client.post("/submit").status_code == 403


def test_the_comparison_helper_handles_non_ascii():
    """The helper the fix uses, exercised directly."""

    assert constant_time_compare(f"caf{NON_ASCII}", f"caf{NON_ASCII}") is True
    assert constant_time_compare(f"caf{NON_ASCII}", "cafe") is False


def test_the_comparison_helper_still_rejects_a_mismatch():

    assert constant_time_compare("token", "other") is False


# ── F1c: PKCE verifier ───────────────────────────────────────────────


def test_a_non_ascii_pkce_verifier_is_refused_not_a_crash():
    """The defect: `verifier.encode("ascii")` raised at the token endpoint."""

    assert _verify_pkce(f"verifier{NON_ASCII}", "any-challenge") is False


def test_a_matching_pkce_verifier_still_verifies():
    """The negative direction: a legitimate S256 pair must still succeed."""
    import hashlib

    verifier = "a" * 64
    challenge = _b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    assert _verify_pkce(verifier, challenge) is True


def test_a_mismatched_pkce_verifier_is_refused():

    assert _verify_pkce("a" * 64, "not-the-challenge") is False


def test_a_non_ascii_pkce_challenge_is_refused():
    """The other operand is equally caller-controlled."""

    assert _verify_pkce("a" * 64, f"challenge{NON_ASCII}") is False


def test_an_empty_pkce_verifier_is_refused():

    assert _verify_pkce("", "challenge") is False


# ── the class of bug, stated once ────────────────────────────────────


@pytest.mark.parametrize(
    "character",
    ["é", "中", "\U0001f600", "́", "Ａ"],
    ids=["latin-1", "cjk", "emoji", "combining", "fullwidth"],
)
def test_no_credential_path_crashes_on_any_non_ascii_input(character: str):
    """One parametrised sweep, so a fix that only handles Latin-1 is caught."""

    token = encode_jwt({"sub": "x"}, "secret")
    header, payload, signature = token.split(".")
    with pytest.raises(JWTError):
        decode_jwt(f"{header}.{character}{payload}.{signature}", "secret", algorithms=["HS256"])

    assert constant_time_compare(f"a{character}", "b") is False
    assert _verify_pkce(f"v{character}", "c") is False
