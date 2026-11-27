"""`verify_password` and `needs_rehash` read the stored format the same way.

Both take the same `method$params$salt$hash` string. Decoded by two independent
copies they can disagree about which strings are well-formed, and the
disagreement is not academic: a verifier `needs_rehash` parses but
`verify_password` rejects is a credential that fails login and is reported as
upgradeable, and the reverse is a credential that logs in and is never
upgraded. One decoder means the two answer from the same parse.
"""

from __future__ import annotations

import pytest

from veloce.passwords import (
    _scrypt_params,
    _split_verifier,
    hash_password,
    needs_rehash,
    verify_password,
)

MALFORMED = [
    "",
    "scrypt",
    "scrypt$32768:8:1",
    "scrypt$32768:8:1$c2FsdA",
    "$$$",
    "$",
    "no-dollars-at-all",
]


@pytest.mark.parametrize("stored", MALFORMED)
def test_both_readers_refuse_the_same_malformed_verifiers(stored: str) -> None:
    assert verify_password(stored, "pw") is False
    assert needs_rehash(stored) is False


def test_a_well_formed_verifier_is_accepted_by_both() -> None:
    stored = hash_password("pw")
    assert verify_password(stored, "pw") is True
    assert needs_rehash(stored) is False


def test_the_fourth_field_keeps_any_further_separators() -> None:
    """A base64 payload cannot contain `$`, but the split must not eat one."""
    assert _split_verifier("m$p$s$h$extra") == ("m", "p", "s", "h$extra")


def test_a_trailing_separator_still_yields_four_fields() -> None:
    assert _split_verifier("m$p$s$") == ("m", "p", "s", "")


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ("32768:8:1", (32768, 8, 1)),
        ("32768:8", None),
        ("32768:8:1:1", None),
        ("a:b:c", None),
        ("", None),
    ],
)
def test_scrypt_params_parses_or_refuses(params: str, expected: tuple | None) -> None:
    assert _scrypt_params(params) == expected


def test_a_weakened_scrypt_verifier_is_refused_and_flagged() -> None:
    """The two readers agreeing is only useful if they still answer correctly."""
    stored = hash_password("pw")
    _, _, salt, digest = _split_verifier(stored)
    tampered = f"scrypt$2:8:1${salt}${digest}"
    # Below the security floor: verify refuses outright...
    assert verify_password(tampered, "pw") is False
    # ...and it is genuinely weaker than the current default, so it is a
    # rehash candidate. Both answers come from one parse of one string.
    assert needs_rehash(tampered) is True
