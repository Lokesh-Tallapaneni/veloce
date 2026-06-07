"""Property-based fuzz tests for the signed-token decoder — `signing.py`.

Feeds arbitrary and corrupted tokens at `Signer.loads`, asserting it only ever
raises `BadSignature` (or its `BadData` / `BadTimeSignature` subclasses) — a
malformed token must never drive the JSON parser or crash. Also asserts the
`dumps` -> `loads` round-trip for JSON-serialisable values.
"""

from __future__ import annotations

import contextlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from veloce.signing import BadSignature, Signer

pytestmark = pytest.mark.fuzz

_SECRET = "fuzz-secret-value"

# JSON-serialisable payloads: the value space `orjson.dumps`/`loads` round-trips.
_json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**15), max_value=10**15)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=60),
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=20), children, max_size=5)
    ),
    max_leaves=15,
)


@given(token=st.text(max_size=200))
def test_loads_arbitrary_text_only_raises_bad_signature(token: str) -> None:
    """An arbitrary token raises only `BadSignature` (or a subclass)."""
    signer = Signer(_SECRET)
    with contextlib.suppress(BadSignature):
        signer.loads(token)


@given(token=st.text(alphabet="abcXYZ0189-_.=", max_size=120))
def test_loads_token_shaped_soup_only_raises_bad_signature(token: str) -> None:
    """Strings built from the token alphabet still only raise `BadSignature`."""
    signer = Signer(_SECRET)
    with contextlib.suppress(BadSignature):
        signer.loads(token)


@given(data=st.binary(max_size=120))
def test_loads_non_str_token_raises_bad_signature(data: bytes) -> None:
    """A non-`str` token is rejected as `BadSignature`, not a `TypeError`."""
    signer = Signer(_SECRET)
    with contextlib.suppress(BadSignature):
        signer.loads(data)  # type: ignore[arg-type]


@given(value=_json_values)
def test_dumps_then_loads_round_trips(value: object) -> None:
    """A signed value decodes back equal (no `max_age`)."""
    signer = Signer(_SECRET)
    token = signer.dumps(value)
    assert signer.loads(token) == value


@given(
    value=_json_values,
    positions=st.lists(st.integers(min_value=0), max_size=5),
    replacements=st.lists(st.sampled_from("abXY09-_."), max_size=5),
)
def test_corrupting_a_valid_token_yields_only_bad_signature_or_original(
    value: object, positions: list[int], replacements: list[str]
) -> None:
    """Corrupting a valid token only raises `BadSignature` or returns the original.

    The HMAC is verified before the payload is decoded, so a tampered token can
    never reach (let alone crash) the JSON parser, and it can never decode to a
    *different* value. It may still verify and return the original value: a
    single base64url-character change does not always alter the decoded bytes
    (trailing padding bits carry no information), so an unchanged signing input
    legitimately validates.
    """
    signer = Signer(_SECRET)
    token = list(signer.dumps(value))
    for pos, rep in zip(positions, replacements):
        token[pos % len(token)] = rep
    candidate = "".join(token)
    try:
        result = signer.loads(candidate)
    except BadSignature:
        return
    assert result == value
