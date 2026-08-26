"""Password hashing tests (SEC13)."""

from __future__ import annotations

import asyncio

import pytest

from veloce import (
    hash_password,
    hash_password_async,
    is_strong_password,
    needs_rehash,
    verify_and_needs_update,
    verify_and_needs_update_async,
    verify_password,
    verify_password_async,
)
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


# ── needs_rehash ──────────────────────────────────────────────────────


def test_needs_rehash_false_for_current_defaults():
    """A hash from the current defaults is at the current work factor."""
    stored = hash_password("hunter2")
    assert needs_rehash(stored) is False


def test_needs_rehash_true_for_pbkdf2_against_scrypt_default():
    """PBKDF2 is not the default method → flagged for migration to scrypt."""
    stored = hash_password("hunter2", method="pbkdf2:sha256")
    assert needs_rehash(stored) is True


def test_needs_rehash_true_for_weaker_scrypt_params():
    """A legacy scrypt hash below the current N is a rehash candidate."""
    salt = _b64encode(b"\x00" * 16)
    derived = _b64encode(b"\x00" * 64)
    weak = f"scrypt$16384:8:1${salt}${derived}"  # N=2**14 < default 2**15
    assert needs_rehash(weak) is True


def test_needs_rehash_false_for_stronger_scrypt_params():
    """A hash already above the defaults must not be downgraded."""
    salt = _b64encode(b"\x00" * 16)
    derived = _b64encode(b"\x00" * 64)
    strong = f"scrypt$131072:8:1${salt}${derived}"  # N=2**17 > default
    assert needs_rehash(strong) is False


def test_needs_rehash_false_for_malformed():
    assert needs_rehash("") is False
    assert needs_rehash("garbage") is False
    assert needs_rehash("only$two$segments") is False
    assert needs_rehash("scrypt$bad:params:x$abc$def") is False


def test_needs_rehash_false_for_unknown_method():
    assert needs_rehash("argon2$3:2:1$YWJj$ZGVm") is False


# ── verify_and_needs_update ───────────────────────────────────────────


def test_verify_and_needs_update_ok_no_upgrade():
    stored = hash_password("hunter2")
    assert verify_and_needs_update(stored, "hunter2") == (True, False)


def test_verify_and_needs_update_ok_with_upgrade():
    """Correct password against a PBKDF2 hash → verify ok, upgrade flagged."""
    stored = hash_password("hunter2", method="pbkdf2:sha256")
    ok, upgrade = verify_and_needs_update(stored, "hunter2")
    assert ok is True
    assert upgrade is True


def test_verify_and_needs_update_wrong_password_no_upgrade():
    """A failed verify never reports needs_update — nothing to upgrade."""
    stored = hash_password("hunter2", method="pbkdf2:sha256")
    assert verify_and_needs_update(stored, "wrong") == (False, False)


def test_verify_and_needs_update_malformed_no_upgrade():
    assert verify_and_needs_update("garbage", "x") == (False, False)


async def test_verify_and_needs_update_async_matches_sync():
    stored = hash_password("hunter2", method="pbkdf2:sha256")
    assert await verify_and_needs_update_async(stored, "hunter2") == (True, True)
    assert await verify_and_needs_update_async(stored, "wrong") == (False, False)


@pytest.mark.asyncio
async def test_hash_and_verify_password_async_round_trip():
    """`hash_password_async` / `verify_password_async` are async-safe
    wrappers around the scrypt KDF. Round-tripping a credential must
    work the same way the sync versions do."""
    stored = await hash_password_async("hunter2")
    assert isinstance(stored, str)
    assert "$" in stored
    assert await verify_password_async(stored, "hunter2") is True
    assert await verify_password_async(stored, "wrong-password") is False


@pytest.mark.asyncio
async def test_hash_password_async_does_not_block_the_loop():
    """A handler calling `hash_password_async` leaves the loop free meanwhile.

    The ticker counts only the turns it gets **while the hash is still in
    flight**, which is the property. Counting turns over a fixed 50 ms window
    and asserting `ticked > 0` - as this did - holds either way: a synchronous
    scrypt blocks the ticker until it finishes and the ticker then runs its
    whole window, so the count is comfortably positive with the loop having been
    stalled throughout.

    Measured on this machine: 35 ticks against the executor hop, 0 against a
    synchronous `hash_password`. The threshold is set far below that gap so a
    loaded machine cannot fail it spuriously, while a lost executor hop takes the
    count to zero.
    """
    hashing_done = asyncio.Event()
    ticks_while_hashing = 0

    async def ticker() -> None:
        nonlocal ticks_while_hashing
        while not hashing_done.is_set():
            await asyncio.sleep(0.001)
            ticks_while_hashing += 1

    async def hasher() -> str:
        try:
            return await hash_password_async("hunter2")
        finally:
            hashing_done.set()

    digest, _ = await asyncio.gather(hasher(), ticker())
    assert ticks_while_hashing >= 3, (
        f"the loop only got {ticks_while_hashing} turns while hashing - "
        "`hash_password_async` is not hopping to a thread"
    )
    # And it still produced a usable hash rather than merely yielding.
    assert verify_password(digest, "hunter2")
