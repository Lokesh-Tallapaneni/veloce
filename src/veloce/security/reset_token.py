"""Reset tokens — storage-free, self-invalidating password-reset links.

A thin layer over ``veloce.signing.Signer`` that binds an opaque
caller-supplied state fingerprint into a one-time, expiring token. No
crypto lives here: signing, timestamping, and constant-time comparison
are all delegated to ``Signer`` (RFC 2104 HMAC). The caller's fingerprint
(typically ``user_id + password_hash + last_login``) makes the token
self-invalidate: when the password changes or the user logs in, the
fingerprint changes and the old token no longer validates.

Usage::

    state = b"".join([str(user.id).encode(), user.password_hash.encode()])
    token = make_reset_token(state, secret=SECRET)
    ...
    if check_reset_token(token, state, secret=SECRET, max_age=3600):
        ...
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence

from veloce.exceptions import VeloceError
from veloce.signing import BadSignature, Signer

RESET_TOKEN_SALT = "veloce.security.reset_token"
_RESET_TOKEN_VERSION = 1


class BadResetToken(VeloceError, TypeError):
    """Raised on programmer misuse; invalid tokens return False instead.

    Also a `TypeError`, which is what the bare misuse raises - so the
    documented `except BadResetToken` works without breaking a caller who
    catches `TypeError` instead.
    """


def _fingerprint(state: bytes) -> str:
    return hashlib.blake2b(bytes(state), digest_size=32).hexdigest()


def make_reset_token(
    state: bytes,
    *,
    secret: str | bytes,
    salt: str | bytes = RESET_TOKEN_SALT,
) -> str:
    """Bind a caller-supplied state fingerprint into a signed reset token."""
    if not isinstance(state, (bytes, bytearray)):
        raise BadResetToken("state must be bytes")
    fp = _fingerprint(bytes(state))
    signer = Signer(secret, salt)
    return signer.dumps([_RESET_TOKEN_VERSION, fp])


def check_reset_token(
    token: str,
    state: bytes,
    *,
    secret: str | bytes,
    max_age: int,
    fallback_secrets: Sequence[str | bytes] = (),
    salt: str | bytes = RESET_TOKEN_SALT,
) -> bool:
    """Return True iff the token is authentic, unexpired, and still bound to `state`."""
    if not isinstance(state, (bytes, bytearray)):
        raise BadResetToken("state must be bytes")
    signer = Signer(secret, salt)
    for fb in fallback_secrets:
        signer.add_fallback_secret(fb, salt)
    try:
        payload = signer.loads(token, max_age=max_age)
    except BadSignature:
        return False
    if not (isinstance(payload, list) and len(payload) == 2 and payload[0] == _RESET_TOKEN_VERSION):
        return False
    expected = _fingerprint(bytes(state))
    return hmac.compare_digest(payload[1], expected)
