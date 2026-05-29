"""Password hashing tests (SEC13)."""

from __future__ import annotations

import pytest

from veloce import hash_password, is_strong_password, verify_password
from veloce._internal import _b64encode

# ── Round-trip ────────────────────────────────────────────────────────


def test_hash_then_verify_scrypt():
    stored = hash_password("hunter2")
    assert stored.startswith("scrypt$")
    assert verify_password(stored, "hunter2") is True


def test_hash_then_verify_pbkdf2():
    stored = hash_password("hunter2", method="pbkdf2:sha256")
    assert stored.startswith("pbkdf2:sha256$")
    assert verify_password(stored, "hunter2") is True


def test_wrong_password_rejected():
    stored = hash_password("correct")
    assert verify_password(stored, "wrong") is False


def test_two_hashes_of_same_password_differ():
    """Salt must make each hash unique even when input is identical."""
    a = hash_password("samepass")
    b = hash_password("samepass")
    assert a != b
    # Both still verify.
    assert verify_password(a, "samepass")
    assert verify_password(b, "samepass")


def test_bytes_password_accepted():
    stored = hash_password(b"binary-pw")
    assert verify_password(stored, b"binary-pw") is True
    assert verify_password(stored, "binary-pw") is True  # equivalent UTF-8


# ── Format ────────────────────────────────────────────────────────────


def test_stored_format_has_four_segments():
    stored = hash_password("x")
    assert stored.count("$") == 3


def test_stored_format_is_url_safe():
    stored = hash_password("x")
    assert "/" not in stored
    assert "+" not in stored


# ── Construction errors ───────────────────────────────────────────────


def test_empty_password_rejected_at_hash_time():
    with pytest.raises(ValueError):
        hash_password("")


def test_short_salt_length_rejected():
    with pytest.raises(ValueError):
        hash_password("x", salt_length=4)


def test_unknown_method_raises_at_hash_time():
    with pytest.raises(ValueError):
        hash_password("x", method="argon2")  # not supported here


# ── Verify is lenient on malformed input ──────────────────────────────


def test_verify_empty_stored_returns_false():
    assert verify_password("", "x") is False


def test_verify_empty_candidate_returns_false():
    stored = hash_password("x")
    assert verify_password(stored, "") is False


def test_verify_malformed_stored_returns_false():
    """Garbage that doesn't split into four segments → False, no raise."""
    assert verify_password("not-a-real-hash", "anything") is False
    assert verify_password("only$two$segments", "x") is False
    assert verify_password("scrypt$bad-params$abc$def", "x") is False


def test_verify_unknown_method_in_stored_returns_false():
    """A stored hash declaring an unknown method must NOT verify, ever."""
    assert verify_password("argon2$3:2:1$YWJj$ZGVm", "anything") is False


def test_verify_uses_constant_time_compare():
    """Sanity: hmac.compare_digest is used; can't directly observe timing,
    but the function should at least not short-circuit on length mismatch
    in a way that leaks (we check that mismatched-length input still
    returns False instead of crashing)."""
    stored = hash_password("x")
    # Mutate the hash segment to a different length.
    parts = stored.split("$")
    parts[-1] = parts[-1][:-2]
    mangled = "$".join(parts)
    assert verify_password(mangled, "x") is False


# ── Cross-method ──────────────────────────────────────────────────────


def test_pbkdf2_stored_does_not_verify_with_scrypt_logic():
    """Stored as pbkdf2 — the verifier picks the right method via the tag."""
    stored = hash_password("x", method="pbkdf2:sha256")
    assert verify_password(stored, "x") is True
    assert verify_password(stored, "y") is False


# ── is_strong_password ────────────────────────────────────────────────


def test_is_strong_password_baseline():
    assert is_strong_password("abc12345")
    assert is_strong_password("a1234567")
    assert not is_strong_password("short1")  # too short
    assert not is_strong_password("12345678")  # no alpha
    assert not is_strong_password("abcdefgh")  # no digit


def test_is_strong_password_custom_min_length():
    assert is_strong_password("a1", min_length=2)
    assert not is_strong_password("a", min_length=2)


# ── Verify-time DoS protection: scrypt upper caps ─────────────────────


def _fake_scrypt_hash(n: int, r: int, p: int) -> str:
    """Build a syntactically valid scrypt hash string without running scrypt.

    Salt and hash bytes are arbitrary fixed bytes of the canonical lengths
    (16-byte salt, 64-byte derived key). The point is that verify_password
    must reject the tampered cost parameters BEFORE invoking scrypt.
    """
    salt = _b64encode(b"\x00" * 16)
    derived = _b64encode(b"\x00" * 64)
    return f"scrypt${n}:{r}:{p}${salt}${derived}"


def test_verify_rejects_excessive_scrypt_n():
    """A tampered N=2**30 would request ~2 TiB maxmem — refuse fast."""
    stored = _fake_scrypt_hash(n=2**30, r=8, p=1)
    assert verify_password(stored, "anything") is False


def test_verify_rejects_excessive_scrypt_r():
    stored = _fake_scrypt_hash(n=2**15, r=10000, p=1)
    assert verify_password(stored, "anything") is False


def test_verify_rejects_excessive_scrypt_p():
    stored = _fake_scrypt_hash(n=2**15, r=8, p=10000)
    assert verify_password(stored, "anything") is False


def test_verify_default_params_still_succeeds():
    """Sanity: hashes produced by hash_password's defaults still verify."""
    stored = hash_password("hunter2")
    assert verify_password(stored, "hunter2") is True


# ── Verify-time DoS protection: PBKDF2 upper cap ──────────────────────


def test_verify_rejects_excessive_pbkdf2_iterations():
    """A tampered iterations=10**12 would pin a verify thread for hours."""
    salt = _b64encode(b"\x00" * 16)
    derived = _b64encode(b"\x00" * 32)
    stored = f"pbkdf2:sha256$1000000000000${salt}${derived}"
    assert verify_password(stored, "anything") is False


def test_verify_pbkdf2_default_iterations_still_succeeds():
    stored = hash_password("hunter2", method="pbkdf2:sha256")
    assert verify_password(stored, "hunter2") is True
