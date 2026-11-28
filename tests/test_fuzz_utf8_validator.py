"""Differential fuzz test — `_Utf8Validator` against the automaton it replaced.

The hand-rolled byte-automaton `_Utf8Validator` used to carry inline is the
only oracle precise enough to trust here: it is a direct transcription of the
Unicode UTF-8 acceptance tables, kept below as the reference rather than in
`websocket.py`, where only the stdlib-backed replacement now lives. Every
fragment split must agree between the two, including the one documented
timing difference the replacement patches for itself - rejecting a surrogate
lead at its second byte rather than its third.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from veloce.websocket import _Utf8Validator

pytestmark = pytest.mark.fuzz


class _ReferenceUtf8Validator:
    """The automaton `_Utf8Validator` used to be, kept here as a fuzz oracle."""

    __slots__ = ("_need", "_lo", "_hi")

    def __init__(self) -> None:
        self._need = 0
        self._lo = 0x80
        self._hi = 0xBF

    @property
    def done(self) -> bool:
        return self._need == 0

    def feed(self, chunk: bytes | bytearray) -> bool:
        need = self._need
        lo = self._lo
        hi = self._hi
        for byte in chunk:
            if need == 0:
                if byte < 0x80:
                    continue
                if 0xC2 <= byte <= 0xDF:
                    need, lo, hi = 1, 0x80, 0xBF
                elif byte == 0xE0:
                    need, lo, hi = 2, 0xA0, 0xBF
                elif 0xE1 <= byte <= 0xEC or byte in (0xEE, 0xEF):
                    need, lo, hi = 2, 0x80, 0xBF
                elif byte == 0xED:
                    need, lo, hi = 2, 0x80, 0x9F
                elif byte == 0xF0:
                    need, lo, hi = 3, 0x90, 0xBF
                elif 0xF1 <= byte <= 0xF3:
                    need, lo, hi = 3, 0x80, 0xBF
                elif byte == 0xF4:
                    need, lo, hi = 3, 0x80, 0x8F
                else:
                    return False
            else:
                if byte < lo or byte > hi:
                    return False
                need -= 1
                lo, hi = 0x80, 0xBF
        self._need = need
        self._lo = lo
        self._hi = hi
        return True


def _drive(validator, data: bytes, steps: list[int]) -> list[bool]:
    """Feed `data` through `validator` in `steps`-sized fragments.

    Returns each fragment's `feed()` result, plus a final `done` check - the
    per-fragment history, not just the end state, is what must agree.
    """
    results: list[bool] = []
    pos = 0
    for step in steps:
        if pos >= len(data):
            break
        ok = validator.feed(data[pos : pos + step])
        results.append(ok)
        pos += step
        if not ok:
            return results
    if pos < len(data):
        ok = validator.feed(data[pos:])
        results.append(ok)
    results.append(validator.done)
    return results


@settings(deadline=None, max_examples=500)
@given(
    data=st.binary(max_size=64),
    steps=st.lists(st.integers(min_value=1, max_value=4), min_size=1, max_size=64),
)
def test_matches_the_reference_automaton_on_arbitrary_fragments(
    data: bytes, steps: list[int]
) -> None:
    assert _drive(_Utf8Validator(), data, steps) == _drive(_ReferenceUtf8Validator(), data, steps)


@settings(deadline=None, max_examples=500)
@given(text=st.text(max_size=48), steps=st.lists(st.integers(min_value=1, max_value=4), min_size=1))
def test_matches_the_reference_automaton_on_valid_unicode_text(text: str, steps: list[int]) -> None:
    """Valid text, not just arbitrary bytes - exercises every multi-byte lead."""
    data = text.encode("utf-8")
    assert _drive(_Utf8Validator(), data, steps) == _drive(_ReferenceUtf8Validator(), data, steps)


def test_a_surrogate_lead_is_rejected_at_its_second_byte_not_its_third():
    """The one documented timing difference the replacement patches for
    itself: U+D800's lead byte 0xED followed by a byte in 0xA0-0xBF commits
    to a surrogate before a third byte could possibly disambiguate it."""
    validator = _Utf8Validator()
    assert validator.feed(bytes([0xED])) is True
    assert validator.feed(bytes([0xA0])) is False
