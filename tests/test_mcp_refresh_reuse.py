"""Replaying a refresh token revokes the chain it belonged to.

Rotation already dropped the old refresh token, so a replay was refused with
`invalid_grant`. That refuses the one request and leaves the *rotated* pair
working — which is exactly the pair a thief holds after using the token they
stole. OAuth 2.1 Sec. 4.14.2 wants the whole chain revoked on detected reuse,
so the legitimate client is forced to re-authorize and the thief's tokens die
with it.

Every token issued by a rotation inherits the `family_id` of the one it
replaced, so "the chain" is a single lookup rather than a graph walk.
"""

from __future__ import annotations

import pytest

from veloce.contrib.mcp.authorization import (
    AccessToken,
    InMemoryAuthorizationStore,
    _digest,
)


def _token(family: str, refresh_digest: str, *, client: str = "c1") -> AccessToken:
    return AccessToken(
        client_id=client,
        subject="alice",
        scopes=frozenset({"read"}),
        resource=None,
        expires_at=1e12,
        refresh_digest=refresh_digest,
        family_id=family,
    )


async def _store_with(family: str, refresh: str) -> tuple[InMemoryAuthorizationStore, str]:
    store = InMemoryAuthorizationStore()
    digest = _digest(refresh)
    await store.save_token(_digest("access-" + refresh), _token(family, digest))
    return store, digest


# ── A family is a real, inherited identity ───────────────────────────


def test_a_token_gets_a_family_by_default():
    token = AccessToken(
        client_id="c1", subject=None, scopes=frozenset(), resource=None, expires_at=0.0
    )
    assert isinstance(token.family_id, str) and token.family_id


def test_two_tokens_start_in_different_families():
    def make() -> AccessToken:
        return AccessToken(
            client_id="c1", subject=None, scopes=frozenset(), resource=None, expires_at=0.0
        )

    assert make().family_id != make().family_id


# ── Spending a refresh token is remembered ───────────────────────────


async def test_taking_a_refresh_token_records_its_family():
    store, digest = await _store_with("fam-1", "r1")
    assert await store.take_refresh(digest) is not None
    assert await store.family_of_spent_refresh(digest) == "fam-1"


async def test_a_never_issued_refresh_token_has_no_family():
    store = InMemoryAuthorizationStore()
    assert await store.family_of_spent_refresh(_digest("never-issued")) is None


async def test_a_refresh_token_can_only_be_taken_once():
    store, digest = await _store_with("fam-1", "r1")
    assert await store.take_refresh(digest) is not None
    assert await store.take_refresh(digest) is None


# ── Revoking a family takes every descendant ─────────────────────────


async def test_revoking_a_family_drops_all_its_tokens():
    store = InMemoryAuthorizationStore()
    for n in range(3):
        await store.save_token(_digest(f"a{n}"), _token("fam-1", _digest(f"r{n}")))
    assert await store.revoke_family("fam-1") == 3
    for n in range(3):
        assert await store.get_token(_digest(f"a{n}")) is None


async def test_revoking_a_family_leaves_other_families_alone():
    store = InMemoryAuthorizationStore()
    await store.save_token(_digest("a1"), _token("fam-1", _digest("r1")))
    await store.save_token(_digest("a2"), _token("fam-2", _digest("r2")))
    await store.revoke_family("fam-1")
    assert await store.get_token(_digest("a1")) is None
    assert await store.get_token(_digest("a2")) is not None


async def test_revoking_a_family_invalidates_its_refresh_tokens():
    """A surviving refresh digest would let the chain be resurrected."""
    store, digest = await _store_with("fam-1", "r1")
    await store.revoke_family("fam-1")
    assert await store.take_refresh(digest) is None


async def test_revoking_clears_the_spent_record_too():
    """Otherwise a revoked family's digests accumulate for the process lifetime."""
    store, digest = await _store_with("fam-1", "r1")
    await store.take_refresh(digest)
    await store.revoke_family("fam-1")
    assert await store.family_of_spent_refresh(digest) is None


async def test_revoking_an_unknown_family_is_a_no_op():
    store = InMemoryAuthorizationStore()
    assert await store.revoke_family("nope") == 0


# ── The reuse signal is distinguishable from a bad token ─────────────


async def test_a_replayed_token_is_reported_as_reuse_not_as_unknown():
    """The grant needs to tell these apart to decide whether to revoke."""
    store, digest = await _store_with("fam-1", "r1")
    await store.take_refresh(digest)
    assert await store.take_refresh(digest) is None
    assert await store.family_of_spent_refresh(digest) == "fam-1"


async def test_an_unknown_token_is_not_reported_as_reuse():
    store = InMemoryAuthorizationStore()
    assert await store.take_refresh(_digest("garbage")) is None
    assert await store.family_of_spent_refresh(_digest("garbage")) is None


# ── A store predating the feature still works ────────────────────────


async def test_a_store_without_the_optional_methods_degrades():
    """Requiring them would break every custom store on upgrade."""
    from veloce.contrib.mcp.authorization import _revoke_family, _spent_family

    class Old:
        """A store written before reuse detection existed."""

    assert await _spent_family(Old(), "digest") is None
    await _revoke_family(Old(), "fam-1")  # must not raise


@pytest.mark.parametrize("family", ["fam-1", "another"])
async def test_rotation_inherits_the_family(family):
    """Without inheritance each rotation would start a new chain and revoke nothing."""
    store, digest = await _store_with(family, "r1")
    _digest_taken, record = await store.take_refresh(digest)  # type: ignore[misc]
    assert record.family_id == family
