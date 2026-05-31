"""signing - HMAC-signed value serialiser.

A standalone payload-signing helper for cookies, password-reset links,
email-confirmation tokens, and similar use cases where the server hands
the client a value, the client returns it later, and the server needs
to know the value wasn't tampered with.

Token format: `<base64url(payload)>.<base64url(timestamp)>.<base64url(sig)>`

- The payload is the user's value serialised to JSON (via `orjson`).
- The timestamp is a big-endian uint64 of seconds since the epoch.
- The signature is `HMAC-SHA256(derived_key, payload.timestamp)` where
  `derived_key = HMAC-SHA256(secret, salt)` (nested HMAC, RFC 2104 Sec. 2).
  The salt is used to derive a per-purpose key, not appended to the
  secret - sharing one secret across purposes (sessions, CSRF, password
  reset) yields cryptographically distinct keys.

Tokens are URL-safe (no `/`, `+`, `=` characters). Comparison is
constant-time. The implementation is derived from RFC 2104 (HMAC) and
RFC 4648 Sec. 5 (base64url); the observable token shape matches
a timestamped signer's `URLSafeTimedSerializer` so swapping is straightforward,
but no a timestamped signer code is copied.

Why an in-tree signer (rather than depending on `itsdangerous`):
Veloce keeps this ~150-line signer instead of taking the `itsdangerous`
dependency - a considered trade-off, not an oversight:

- Signing rests entirely on the standard library (`hmac`, `hashlib`,
  `base64`, `struct`), so the dependency surface stays minimal and the
  whole implementation is small enough to audit in one sitting.
- The security-critical properties are deliberately narrow and covered
  by `tests/test_signing.py`: HMAC-SHA256, a salt mixed into the MAC key
  (so tokens for different purposes never cross-validate), constant-time
  comparison, and - importantly - signature verified *before* the
  timestamp and payload are decoded, so a malformed token cannot drive
  the JSON parser ahead of the MAC check.
- The token shape is wire-compatible with `URLSafeTimedSerializer`, so
  migrating to `itsdangerous` later (should that ever be wanted) is a
  low-risk drop-in.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
import time
from typing import Any

import orjson

from veloce._internal import _b64decode, _b64encode

# -- Exceptions ---------------------------------------------------


class BadSignature(Exception):
    """The token's signature did not verify against the configured secret."""

    pass


class BadTimeSignature(BadSignature):
    """The signature verified but the token is older than `max_age`."""

    pass


class BadData(BadSignature):
    """The token's payload could not be decoded (malformed base64 / JSON)."""

    pass


class Signer:
    """HMAC-SHA256 signer for arbitrary JSON-serialisable values.

    Usage:
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
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if isinstance(salt, str):
            salt = salt.encode("utf-8")
        if not secret:
            raise ValueError("secret must be non-empty")
        # Derive a per-salt key from the secret. Mixing the salt into the
        # MAC key means tokens for different purposes (e.g. session vs
        # password-reset) won't validate against each other even though
        # they share the secret.
        self._key = hmac.new(secret, salt, hashlib.sha256).digest()
        # Future support for key rotation: extra accepted keys for verify.
        self._secret_keys: list[bytes] = [self._key]

    # -- Key rotation -----------------------------------------------

    def add_fallback_secret(
        self, secret: str | bytes, salt: str | bytes = "veloce.signing"
    ) -> None:
        """Add an additional secret accepted for verification (not signing).

        Used during secret rotation: configure the new secret as primary,
        keep the old one as a fallback for the rotation window. Tokens
        signed with the fallback still verify; new tokens use the primary.
        """
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if isinstance(salt, str):
            salt = salt.encode("utf-8")
        self._secret_keys.append(hmac.new(secret, salt, hashlib.sha256).digest())

    # -- dumps / loads ----------------------------------------------

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
        # Single split-with-cap instead of `count(".") != 2` + `split(".")`.
        # `split(".", 2)` returns at most 3 parts: a well-formed token gives
        # exactly 3, a truncated token gives fewer. A token with extra dots
        # beyond the second packs them into `sig_b64`; `urlsafe_b64decode`
        # tolerates non-alphabet characters (binascii's relaxed mode strips
        # them), so the segment decodes to nonsense, the HMAC compare fails,
        # and the caller sees `BadSignature` instead of `BadData`. Both are
        # caught by `except BadSignature`, so the change of diagnostic does
        # not affect handlers.
        parts = token.split(".", 2)
        if len(parts) != 3:
            raise BadData(f"malformed token: {token!r}")
        payload_b64, ts_b64, sig_b64 = parts
        try:
            sig_bytes = _b64decode(sig_b64)
        except (ValueError, OSError) as err:
            raise BadData("malformed signature") from err

        signing_input = f"{payload_b64}.{ts_b64}".encode("ascii")
        # Try every accepted secret. Constant-time compare per attempt.
        if not any(
            hmac.compare_digest(sig_bytes, hmac.new(key, signing_input, hashlib.sha256).digest())
            for key in self._secret_keys
        ):
            raise BadSignature("signature does not match")

        # Timestamp + payload decode happens AFTER signature verification so
        # an attacker can't probe parser behaviour with malformed inputs.
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
