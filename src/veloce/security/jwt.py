"""Stdlib JWT — compact JWS sign/verify for the HMAC family.

A dependency-free JSON Web Token implementation covering the HMAC-SHA2
algorithms (HS256/HS384/HS512) per RFC 7515 (JWS), RFC 7519 (JWT), and
RFC 7518 (JWA), on the standard library alone (``hmac``/``hashlib`` per
RFC 2104, base64url per RFC 4648 Sec. 5).

Scope is deliberately HMAC-only; no RSA/EC algorithms are supported. The
signature is always verified before the payload JSON is decoded, so the parser
is never driven with unauthenticated bytes, the ``alg`` allow-list is required
with no default, and ``alg: "none"`` is rejected unconditionally to prevent
algorithm confusion.

Usage::

    token = encode_jwt({"sub": "42", "exp": 1893456000}, "secret")
    claims = decode_jwt(token, "secret", algorithms=["HS256"])
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import orjson

from veloce._internal import _b64decode, _b64encode, _coerce_secret_bytes
from veloce.exceptions import VeloceError

# RFC 7518 Sec. 3.2 - the HMAC family. Not user-extensible: a requested
# algorithm outside this map raises UnsupportedAlgorithmError.
_ALGORITHMS: dict[str, Any] = {
    "HS256": hashlib.sha256,
    "HS384": hashlib.sha384,
    "HS512": hashlib.sha512,
}

# The `typ` member of the JWS header (RFC 7515 Sec. 4.1.9).
_HEADER_TYP = "JWT"


# ── Errors ──────────────────────────────────────────────────────


class JWTError(VeloceError):
    """Base class for all JWT decode/encode failures."""


class InvalidTokenError(JWTError):
    """Malformed structure: not three segments, bad base64, or bad JSON."""


class InvalidSignatureError(JWTError):
    """The HMAC signature did not verify against the secret."""


class UnsupportedAlgorithmError(JWTError):
    """The header `alg` is not allow-listed, unknown, or `none`."""


class ExpiredSignatureError(JWTError):
    """The token's `exp` claim is in the past (beyond leeway)."""


class ImmatureSignatureError(JWTError):
    """The token's `nbf` claim is in the future (beyond leeway)."""


class InvalidAudienceError(JWTError):
    """The `aud` claim does not match the expected audience."""


class InvalidIssuerError(JWTError):
    """The `iss` claim does not match the expected issuer."""


class MissingClaimError(JWTError):
    """A claim named in `require` is absent from the payload."""


# ── Claims ──────────────────────────────────────────────────────


class Claims(Mapping[str, Any]):
    """Read-only mapping over a decoded JWT payload.

    Usage::

        claims = decode_jwt(token, secret, algorithms=["HS256"])
        user_id = claims["sub"]
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"Claims({self._data!r})"


# ── encode / decode ─────────────────────────────────────────────


def encode_jwt(
    claims: Mapping[str, Any],
    secret: str | bytes,
    *,
    alg: str = "HS256",
) -> str:
    """Sign `claims` into a compact JWS token using the given HMAC algorithm."""
    digestmod = _ALGORITHMS.get(alg)
    if digestmod is None:
        raise UnsupportedAlgorithmError(f"unsupported algorithm {alg!r}")
    secret_bytes = _coerce_secret_bytes(secret)
    if not secret_bytes:
        raise ValueError("secret must be non-empty")
    header_b64 = _b64encode(orjson.dumps({"alg": alg, "typ": _HEADER_TYP}))
    payload_b64 = _b64encode(orjson.dumps(dict(claims)))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(secret_bytes, signing_input, digestmod).digest()
    return f"{header_b64}.{payload_b64}.{_b64encode(sig)}"


def _validate_claims(
    payload: dict[str, Any],
    *,
    audience: str | Sequence[str] | None,
    issuer: str | None,
    require: Sequence[str],
    leeway: float,
    now: float | None,
) -> None:
    """Apply the RFC 7519 Sec. 4.1 registered-claim rules to a verified payload.

    `decode_jwt` calls it only after the signature has been verified.
    """
    current = time.time() if now is None else now

    exp = payload.get("exp")
    if exp is not None:
        if not isinstance(exp, (int, float)) or isinstance(exp, bool):
            raise InvalidTokenError("exp claim is not numeric")
        if exp <= current - leeway:
            raise ExpiredSignatureError("token has expired")

    nbf = payload.get("nbf")
    if nbf is not None:
        if not isinstance(nbf, (int, float)) or isinstance(nbf, bool):
            raise InvalidTokenError("nbf claim is not numeric")
        if nbf > current + leeway:
            raise ImmatureSignatureError("token is not yet valid")

    if issuer is not None and payload.get("iss") != issuer:
        raise InvalidIssuerError("issuer mismatch")

    if audience is not None:
        accepted = {audience} if isinstance(audience, str) else set(audience)
        claim_aud = payload.get("aud")
        if claim_aud is None:
            raise InvalidAudienceError("audience claim is missing")
        # RFC 7519 Sec. 4.1.3: `aud` is one StringOrURI or an array of them. The
        # shape is checked before coercion so a malformed claim is a 401, not a 500.
        if isinstance(claim_aud, str):
            present = {claim_aud}
        elif isinstance(claim_aud, Sequence) and all(isinstance(a, str) for a in claim_aud):
            present = set(claim_aud)
        else:
            raise InvalidAudienceError("audience claim is not a string or list of strings")
        if accepted.isdisjoint(present):
            raise InvalidAudienceError("audience mismatch")

    for name in require:
        if name not in payload:
            raise MissingClaimError(f"missing required claim {name!r}")


def decode_jwt(
    token: str,
    secret: str | bytes,
    *,
    algorithms: Sequence[str],
    audience: str | Sequence[str] | None = None,
    issuer: str | None = None,
    require: Sequence[str] = (),
    leeway: float = 0,
    now: float | None = None,
) -> Claims:
    """Verify a compact JWS token and return its claims as a read-only mapping."""
    if not algorithms:
        raise ValueError("algorithms allow-list is required and must be non-empty")
    secret_bytes = _coerce_secret_bytes(secret)
    # RFC 7519/RFC 2104: an empty HMAC key would verify tokens signed with the
    # empty secret, so it raises loudly rather than a JWTError swallowed as a 401.
    if not secret_bytes:
        raise ValueError("secret must be non-empty")

    parts = token.split(".", 3)
    if len(parts) != 3:
        raise InvalidTokenError("token does not have exactly three segments")
    header_b64, payload_b64, sig_b64 = parts

    try:
        header = orjson.loads(_b64decode(header_b64))
    except (ValueError, OSError, orjson.JSONDecodeError) as err:
        raise InvalidTokenError("malformed header segment") from err
    if not isinstance(header, dict):
        raise InvalidTokenError("header is not a JSON object")

    alg = header.get("alg")
    # `alg: none` and any non-allow-listed / unknown algorithm are rejected
    # BEFORE any signature work, defeating algorithm-confusion attacks.
    if alg == "none" or alg not in algorithms or alg not in _ALGORITHMS:
        raise UnsupportedAlgorithmError(f"algorithm {alg!r} is not accepted")

    try:
        sig = _b64decode(sig_b64)
    except (ValueError, OSError) as err:
        raise InvalidTokenError("malformed signature segment") from err

    # The segments are ASCII (RFC 7515 Sec. 2), but `_b64decode` is relaxed enough
    # to parse a stray non-ASCII byte, so encoding here is a 401 and not a 500.
    try:
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    except UnicodeEncodeError as err:
        raise InvalidTokenError("token segments must be ASCII") from err
    expected = hmac.new(secret_bytes, signing_input, _ALGORITHMS[alg]).digest()
    # Signature verified BEFORE decoding the payload: never drive the JSON
    # parser with unauthenticated bytes.
    if not hmac.compare_digest(sig, expected):
        raise InvalidSignatureError("signature verification failed")

    try:
        payload = orjson.loads(_b64decode(payload_b64))
    except (ValueError, OSError, orjson.JSONDecodeError) as err:
        raise InvalidTokenError("malformed payload segment") from err
    if not isinstance(payload, dict):
        raise InvalidTokenError("payload is not a JSON object")

    _validate_claims(
        payload, audience=audience, issuer=issuer, require=require, leeway=leeway, now=now
    )
    return Claims(payload)
