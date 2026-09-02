"""HMAC-signed value serialiser tests (SEC14)."""

from __future__ import annotations

import time

import pytest

from veloce import BadData, BadSignature, BadTimeSignature, Signer

# ── Round-trip ────────────────────────────────────────────────────────


def test_roundtrip_dict():
    s = Signer(secret="secret-key")
    token = s.dumps({"user_id": 42, "role": "admin"})
    assert s.loads(token) == {"user_id": 42, "role": "admin"}


def test_roundtrip_string():
    s = Signer(secret="secret-key")
    token = s.dumps("hello world")
    assert s.loads(token) == "hello world"


def test_roundtrip_list():
    s = Signer(secret="secret-key")
    token = s.dumps([1, 2, 3])
    assert s.loads(token) == [1, 2, 3]


def test_token_is_url_safe():
    """The emitted token uses URL-safe base64 with no `=` padding."""
    s = Signer(secret="secret-key")
    token = s.dumps({"x": 1})
    assert "/" not in token
    assert "+" not in token
    assert "=" not in token


def test_token_has_three_dot_separated_segments():
    s = Signer(secret="secret-key")
    token = s.dumps({"x": 1})
    assert token.count(".") == 2


# ── Tamper detection ──────────────────────────────────────────────────


def test_tampered_signature_raises_bad_signature():
    s = Signer(secret="secret-key")
    token = s.dumps({"x": 1})
    # Substitute the entire signature with a known-bad one. Flipping a
    # single base64 char near the tail can occasionally produce a valid
    # alias due to padding-bit equivalence; replacing the whole segment
    # avoids that flake.
    payload, ts, _sig = token.split(".")
    bad_sig = "A" * len(_sig)
    with pytest.raises(BadSignature):
        s.loads(f"{payload}.{ts}.{bad_sig}")


def test_tampered_payload_raises_bad_signature():
    s = Signer(secret="secret-key")
    token = s.dumps({"role": "user"})
    payload, ts, sig = token.split(".")
    # Substitute a different valid payload — the sig won't verify.
    other = Signer(secret="secret-key").dumps({"role": "admin"})
    other_payload = other.split(".")[0]
    with pytest.raises(BadSignature):
        s.loads(f"{other_payload}.{ts}.{sig}")


def test_different_secret_rejects_token():
    """A token signed with secret-A must not verify with secret-B."""
    a = Signer(secret="secret-A")
    b = Signer(secret="secret-B")
    token = a.dumps({"x": 1})
    with pytest.raises(BadSignature):
        b.loads(token)


def test_different_salt_rejects_token():
    """Same secret + different salt produces incompatible tokens."""
    a = Signer(secret="secret", salt="purpose-A")
    b = Signer(secret="secret", salt="purpose-B")
    token = a.dumps({"x": 1})
    with pytest.raises(BadSignature):
        b.loads(token)


# ── Malformed inputs ──────────────────────────────────────────────────


def test_garbage_input_with_three_segments_fails_signature():
    """A random 3-dot-separated string passes the segment-count check
    but its 'signature' won't HMAC-verify — must raise BadSignature."""
    s = Signer(secret="x")
    with pytest.raises(BadSignature):
        s.loads("aaaa.bbbb.cccc")


def test_wrong_segment_count_raises_bad_data():
    s = Signer(secret="x")
    with pytest.raises(BadData):
        s.loads("only-two.segments")
    with pytest.raises(BadData):
        s.loads("")


def test_non_string_input_raises_bad_data():
    s = Signer(secret="x")
    with pytest.raises(BadData):
        s.loads(b"bytes-input")  # type: ignore[arg-type]


# ── max_age ────────────────────────────────────────────────────────────


def test_max_age_allows_fresh_token():
    s = Signer(secret="x")
    token = s.dumps({"u": 1})
    assert s.loads(token, max_age=3600) == {"u": 1}


def test_max_age_rejects_stale_token(monkeypatch):
    """Sign at t=0, validate at t=10000 with max_age=3600 → expired."""
    s = Signer(secret="x")
    now = [1_700_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    token = s.dumps({"u": 1})
    now[0] += 10_000
    with pytest.raises(BadTimeSignature):
        s.loads(token, max_age=3600)


def test_max_age_none_disables_expiry(monkeypatch):
    """max_age=None means timestamps are not checked even after many years."""
    s = Signer(secret="x")
    now = [1_700_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    token = s.dumps({"u": 1})
    now[0] += 1_000_000_000  # ~30 years
    assert s.loads(token, max_age=None) == {"u": 1}


def test_bad_time_signature_is_subclass_of_bad_signature():
    """BadTimeSignature isinstance of BadSignature so a single
    `except BadSignature` catches both tamper and expiry."""
    assert issubclass(BadTimeSignature, BadSignature)


# ── Construction ──────────────────────────────────────────────────────


def test_empty_secret_rejected():
    with pytest.raises(ValueError):
        Signer(secret="")


def test_bytes_secret_and_salt_accepted():
    s = Signer(secret=b"binary-secret", salt=b"binary-salt")
    token = s.dumps({"x": 1})
    assert s.loads(token) == {"x": 1}


# ── Key rotation ──────────────────────────────────────────────────────


def test_add_fallback_secret_accepts_old_tokens():
    """Migrate from `old-secret` to `new-secret`: keep the old one as
    fallback so tokens signed under it still verify."""
    old = Signer(secret="old-secret")
    token = old.dumps({"u": 7})

    new = Signer(secret="new-secret")
    # Without the fallback configured, the old token doesn't verify.
    with pytest.raises(BadSignature):
        new.loads(token)

    new.add_fallback_secret("old-secret")
    # With fallback, it verifies.
    assert new.loads(token) == {"u": 7}


def test_new_tokens_use_primary_secret_only():
    """After rotation, newly-signed tokens come from the primary secret."""
    new = Signer(secret="new")
    new.add_fallback_secret("old")
    token = new.dumps({"u": 1})

    # An exclusively-old Signer can't verify a token freshly signed by
    # the rotated key.
    only_old = Signer(secret="old")
    with pytest.raises(BadSignature):
        only_old.loads(token)


def test_an_empty_fallback_secret_is_refused():
    """NEGATIVE: an empty verification key accepts anyone's forgery.

    `Signer("real")` rejects `""` as a primary secret. As a fallback the same
    value was accepted and derived a key from `b""`, which anyone can compute
    from the published source.
    """
    signer = Signer("a-real-secret")
    with pytest.raises(ValueError, match="fallback secret must be non-empty"):
        signer.add_fallback_secret("")


def test_an_empty_bytes_fallback_secret_is_refused():
    """NEGATIVE: the bytes spelling reaches the same key derivation."""
    signer = Signer("a-real-secret")
    with pytest.raises(ValueError, match="fallback secret must be non-empty"):
        signer.add_fallback_secret(b"")


def test_a_real_fallback_secret_still_verifies_an_old_token():
    """POSITIVE: rotation is the feature; it must keep working.

    A token signed under the previous secret must still verify after the new
    secret becomes primary.
    """
    old = Signer("previous-secret", salt="veloce.session")
    token = old.dumps({"user_id": 7})

    rotated = Signer("current-secret", salt="veloce.session")
    rotated.add_fallback_secret("previous-secret", salt="veloce.session")

    assert rotated.loads(token) == {"user_id": 7}
