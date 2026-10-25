"""signing — HMAC-signed value serialiser.

For cookies, password-reset links, confirmation tokens: the server hands the
client a value, the client returns it, and the server needs to know it was not
tampered with.

Token format: `<base64url(payload)>.<base64url(timestamp)>.<base64url(sig)>`

- The payload is the user's value serialised to JSON (via `orjson`).
- The timestamp is a big-endian uint64 of seconds since the epoch.
- The signature is `HMAC-SHA256(derived_key, payload.timestamp)` where
  `derived_key = HMAC-SHA256(secret, salt)` (nested HMAC, RFC 2104 Sec. 2).
  The salt derives a per-purpose key rather than being appended to the secret,
  so one secret shared across purposes (sessions, CSRF, password reset) yields
  cryptographically distinct keys that never cross-validate.

Tokens are URL-safe (RFC 4648 Sec. 5 base64url, no `/`, `+`, `=`) and
comparison is constant-time. The token shape is wire-compatible with
`URLSafeTimedSerializer`, but no timestamped-signer code is copied.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
import time
from typing import Any

import orjson

from veloce._internal import _b64decode, _b64encode, _coerce_secret_bytes
from veloce.exceptions import VeloceError

# ── Exceptions ──────────────────────────────────────────────


class BadSignature(VeloceError):
    """The token's signature did not verify against the configured secret."""


class BadTimeSignature(BadSignature):
    """The signature verified but the token is older than `max_age`."""


class BadData(BadSignature):
    """The token's payload could not be decoded (malformed base64 / JSON)."""


def _derive_key(secret: bytes, salt: bytes) -> bytes:
    """Derive a per-salt MAC key from the secret via nested HMAC (RFC 2104 Sec. 2).

    Tokens for different purposes then never validate against each other, even
    though they share the secret.
    """
    return hmac.new(secret, salt, hashlib.sha256).digest()


class Signer:
    """HMAC-SHA256 signer for arbitrary JSON-serialisable values.

    Usage::

        s = Signer(secret="server-secret", salt="reset-token")
        token = s.dumps({"user_id": 42})
        ...
        data = s.loads(token, max_age=3600)  # raises if older than 1h
    """

    __slots__ = ("_key", "_secret_keys")

    def __init__(
        self,
        secret: str | bytes,
        salt: str | bytes = "veloce.signing",
    ) -> None:
        secret = _coerce_secret_bytes(secret)
        salt = _coerce_secret_bytes(salt)
        if not secret:
            raise ValueError("secret must be non-empty")
        self._key = _derive_key(secret, salt)
        # Extra keys accepted for verification during secret rotation.
        self._secret_keys: list[bytes] = [self._key]

    # ── Key rotation ──────────────────────────────────────

    def add_fallback_secret(
        self, secret: str | bytes, salt: str | bytes = "veloce.signing"
    ) -> None:
        """Add an additional secret accepted for verification (not signing).

        Tokens signed with the fallback still verify; new tokens use the primary.
        """
        secret = _coerce_secret_bytes(secret)
        salt = _coerce_secret_bytes(salt)
        if not secret:
            # A verification key derived from `b""` accepts anything MACed with
            # a key computable from published source, so refuse it as `__init__` does.
            raise ValueError("fallback secret must be non-empty")
        self._secret_keys.append(_derive_key(secret, salt))

    # ── dumps / loads ─────────────────────────────────────

    def dumps(self, data: Any) -> str:
        """Serialise `data` to a signed, timestamped, URL-safe token."""
        payload_bytes = orjson.dumps(data)
        ts_bytes = struct.pack(">Q", int(time.time()))
        payload_b64 = _b64encode(payload_bytes)
        ts_b64 = _b64encode(ts_bytes)
        signing_input = f"{payload_b64}.{ts_b64}".encode("ascii")
        sig = hmac.new(self._key, signing_input, hashlib.sha256).digest()
        return f"{payload_b64}.{ts_b64}.{_b64encode(sig)}"

    def loads(self, token: str, max_age: int | None = None) -> Any:
        """Verify `token` and return the original data.

        Raises `BadSignature` on tamper / unknown secret, `BadTimeSignature`
        when `max_age` is set and the token's timestamp is older than that.
        """
        if not isinstance(token, str):
            raise BadData(f"malformed token: {token!r}")
        # Dots past the second pack into `sig_b64`, which `urlsafe_b64decode`
        # strips to nonsense, so the HMAC compare fails with `BadSignature`.
        parts = token.split(".", 2)
        if len(parts) != 3:
            raise BadData(f"malformed token: {token!r}")
        payload_b64, ts_b64, sig_b64 = parts
        try:
            sig_bytes = _b64decode(sig_b64)
        except (ValueError, OSError) as err:
            raise BadData("malformed signature") from err

        # A non-ASCII segment is a malformed token, not an `ascii`-codec crash.
        try:
            signing_input = f"{payload_b64}.{ts_b64}".encode("ascii")
        except UnicodeEncodeError as err:
            raise BadData("token segments must be ASCII") from err
        # Constant-time compare per accepted secret.
        if not any(
            hmac.compare_digest(sig_bytes, hmac.new(key, signing_input, hashlib.sha256).digest())
            for key in self._secret_keys
        ):
            raise BadSignature("signature does not match")

        # Decoded only AFTER the MAC check, so a malformed token cannot drive
        # the timestamp or JSON parser as an oracle.
        try:
            ts_bytes = _b64decode(ts_b64)
        except (ValueError, OSError) as err:
            raise BadData("malformed timestamp segment") from err
        if len(ts_bytes) != 8:
            raise BadData("timestamp segment must be 8 bytes")
        signed_at = struct.unpack(">Q", ts_bytes)[0]

        if max_age is not None:
            age = int(time.time()) - signed_at
            if age < 0:
                raise BadTimeSignature("Signature age is negative (future-dated)")
            if age > max_age:
                raise BadTimeSignature(f"token is {age} s old, max_age is {max_age} s")

        try:
            payload_bytes = _b64decode(payload_b64)
        except (ValueError, OSError) as err:
            raise BadData("malformed payload segment") from err
        try:
            return orjson.loads(payload_bytes)
        except orjson.JSONDecodeError as err:
            raise BadData("payload is not valid JSON") from err
