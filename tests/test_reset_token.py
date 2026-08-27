"""Tests for password-reset tokens."""

from __future__ import annotations

import pytest

import veloce.signing
from veloce import check_reset_token, make_reset_token
from veloce.security.reset_token import RESET_TOKEN_SALT
from veloce.signing import Signer

SECRET = "server-secret"
STATE_A = b"user42:hashA"
STATE_B = b"user42:hashB"


def test_roundtrip_valid():
    token = make_reset_token(STATE_A, secret=SECRET)
    assert check_reset_token(token, STATE_A, secret=SECRET, max_age=3600) is True


def test_state_change_invalidates():
    token = make_reset_token(STATE_A, secret=SECRET)
    assert check_reset_token(token, STATE_B, secret=SECRET, max_age=3600) is False


def test_expired_returns_false(monkeypatch):
    """Mint the token in the past rather than sleeping into the future.

    `Signer` stamps and checks whole seconds, so proving `max_age=0` expiry by
    real elapsed time meant `time.sleep(1.1)` - a second and a bit of wall clock
    for one assertion, and the module's whole runtime. Moving the clock back for
    the minting call is exact and instant.
    """

    real = veloce.signing.time.time
    monkeypatch.setattr(veloce.signing.time, "time", lambda: real() - 600)
    token = make_reset_token(STATE_A, secret=SECRET)
    monkeypatch.undo()

    assert check_reset_token(token, STATE_A, secret=SECRET, max_age=0) is False


def test_tampered_token_returns_false():
    token = make_reset_token(STATE_A, secret=SECRET)
    # Flip a char in the signature segment.
    parts = token.split(".")
    sig = parts[2]
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    parts[2] = flipped
    assert check_reset_token(".".join(parts), STATE_A, secret=SECRET, max_age=3600) is False


def test_wrong_secret_returns_false():
    token = make_reset_token(STATE_A, secret=SECRET)
    assert check_reset_token(token, STATE_A, secret="other", max_age=3600) is False


def test_secret_rotation_fallback():
    token = make_reset_token(STATE_A, secret="old-secret")
    assert (
        check_reset_token(
            token, STATE_A, secret="new-secret", max_age=3600, fallback_secrets=["old-secret"]
        )
        is True
    )
    assert check_reset_token(token, STATE_A, secret="new-secret", max_age=3600) is False


def test_salt_isolation():
    token = make_reset_token(STATE_A, secret=SECRET, salt="reset")
    assert check_reset_token(token, STATE_A, secret=SECRET, max_age=3600, salt="confirm") is False


def test_non_bytes_state_raises_typeerror():
    with pytest.raises(TypeError):
        make_reset_token("x", secret=SECRET)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        check_reset_token("tok", "x", secret=SECRET, max_age=10)  # type: ignore[arg-type]


def test_version_mismatch_returns_false():

    signer = Signer(SECRET, RESET_TOKEN_SALT)
    import hashlib

    fp = hashlib.blake2b(STATE_A, digest_size=32).hexdigest()
    token = signer.dumps([99, fp])
    assert check_reset_token(token, STATE_A, secret=SECRET, max_age=3600) is False


def test_public_exports():
    from veloce.security import make_reset_token as _mrt  # noqa: F401
