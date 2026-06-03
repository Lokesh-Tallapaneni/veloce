"""Tests for the stdlib JWT module."""

from __future__ import annotations

import time

import pytest

from veloce import decode_jwt, encode_jwt
from veloce._internal import _b64encode
from veloce.security.jwt import (
    Claims,
    ExpiredSignatureError,
    ImmatureSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    InvalidTokenError,
    MissingClaimError,
    UnsupportedAlgorithmError,
)

SECRET = "topsecret"


@pytest.mark.parametrize("alg", ["HS256", "HS384", "HS512"])
def test_round_trip(alg):
    claims = {"sub": "42", "name": "Ada"}
    token = encode_jwt(claims, SECRET, alg=alg)
    decoded = decode_jwt(token, SECRET, algorithms=[alg])
    assert isinstance(decoded, Claims)
    assert decoded["sub"] == "42"
    assert decoded["name"] == "Ada"
    assert dict(decoded) == claims


def test_claims_read_only():
    decoded = decode_jwt(encode_jwt({"a": 1}, SECRET), SECRET, algorithms=["HS256"])
    with pytest.raises(TypeError):
        decoded["a"] = 2  # type: ignore[index]


def test_alg_none_rejected():
    header = _b64encode(b'{"alg":"none","typ":"JWT"}')
    payload = _b64encode(b'{"sub":"x"}')
    token = f"{header}.{payload}."
    with pytest.raises(UnsupportedAlgorithmError):
        decode_jwt(token, SECRET, algorithms=["HS256"])


def test_allow_list_enforced():
    token = encode_jwt({"a": 1}, SECRET, alg="HS256")
    with pytest.raises(UnsupportedAlgorithmError):
        decode_jwt(token, SECRET, algorithms=["HS384"])


def test_empty_algorithms_raises_value_error():
    token = encode_jwt({"a": 1}, SECRET)
    with pytest.raises(ValueError):
        decode_jwt(token, SECRET, algorithms=())


def test_tampered_payload():
    token = encode_jwt({"sub": "42"}, SECRET)
    header_b64, payload_b64, sig_b64 = token.split(".")
    forged = _b64encode(b'{"sub":"99"}')
    tampered = f"{header_b64}.{forged}.{sig_b64}"
    with pytest.raises(InvalidSignatureError):
        decode_jwt(tampered, SECRET, algorithms=["HS256"])


def test_wrong_secret():
    token = encode_jwt({"a": 1}, SECRET)
    with pytest.raises(InvalidSignatureError):
        decode_jwt(token, "other", algorithms=["HS256"])


def test_malformed_tokens():
    with pytest.raises(InvalidTokenError):
        decode_jwt("a.b", SECRET, algorithms=["HS256"])
    with pytest.raises(InvalidTokenError):
        decode_jwt("!!!.b.c", SECRET, algorithms=["HS256"])
    # Non-JSON header but proper base64.
    header = _b64encode(b"notjson")
    payload = _b64encode(b'{"a":1}')
    bad = f"{header}.{payload}.{_b64encode(b'sig')}"
    with pytest.raises(InvalidTokenError):
        decode_jwt(bad, SECRET, algorithms=["HS256"])


def test_expired():
    past = time.time() - 100
    token = encode_jwt({"exp": past}, SECRET)
    with pytest.raises(ExpiredSignatureError):
        decode_jwt(token, SECRET, algorithms=["HS256"])
    # Leeway widens the acceptance window.
    decode_jwt(token, SECRET, algorithms=["HS256"], leeway=200)


def test_nbf_future():
    future = time.time() + 100
    token = encode_jwt({"nbf": future}, SECRET)
    with pytest.raises(ImmatureSignatureError):
        decode_jwt(token, SECRET, algorithms=["HS256"])
    decode_jwt(token, SECRET, algorithms=["HS256"], leeway=200)


def test_audience():
    token = encode_jwt({"aud": "api"}, SECRET)
    with pytest.raises(InvalidAudienceError):
        decode_jwt(token, SECRET, algorithms=["HS256"], audience="other")
    decode_jwt(token, SECRET, algorithms=["HS256"], audience="api")
    # List aud
    token2 = encode_jwt({"aud": ["api", "web"]}, SECRET)
    decode_jwt(token2, SECRET, algorithms=["HS256"], audience="web")
    # Missing aud when required
    token3 = encode_jwt({"sub": "x"}, SECRET)
    with pytest.raises(InvalidAudienceError):
        decode_jwt(token3, SECRET, algorithms=["HS256"], audience="api")


def test_issuer():
    token = encode_jwt({"iss": "veloce"}, SECRET)
    with pytest.raises(InvalidIssuerError):
        decode_jwt(token, SECRET, algorithms=["HS256"], issuer="other")
    decode_jwt(token, SECRET, algorithms=["HS256"], issuer="veloce")


def test_require_missing():
    token = encode_jwt({"a": 1}, SECRET)
    with pytest.raises(MissingClaimError):
        decode_jwt(token, SECRET, algorithms=["HS256"], require=("sub",))


def test_signature_verified_before_payload_decode():
    # Valid signature over a non-JSON payload -> InvalidTokenError.
    header_b64 = _b64encode(b'{"alg":"HS256","typ":"JWT"}')
    payload_b64 = _b64encode(b"notjson")
    import hashlib
    import hmac

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(SECRET.encode(), signing_input, hashlib.sha256).digest()
    token = f"{header_b64}.{payload_b64}.{_b64encode(sig)}"
    with pytest.raises(InvalidTokenError):
        decode_jwt(token, SECRET, algorithms=["HS256"])
    # Bad signature over garbage payload -> InvalidSignatureError (proves order).
    bad_token = f"{header_b64}.{payload_b64}.{_b64encode(b'badsig')}"
    with pytest.raises(InvalidSignatureError):
        decode_jwt(bad_token, SECRET, algorithms=["HS256"])


def test_import_surface():
    from veloce import JWTError as TopJWTError
    from veloce import decode_jwt as _dj  # noqa: F401
    from veloce import encode_jwt as _ej  # noqa: F401

    assert issubclass(InvalidSignatureError, TopJWTError)
