"""Query/Path constraint validation — ge/le/gt/lt/min_length/max_length/pattern."""

from __future__ import annotations

import pytest

from veloce import Query

# ── numeric: ge / le ────────────────────────────────────────────────


def test_ge_accepts_equal_and_above():
    q = Query(ge=1)
    assert q.validate(1, "x") == 1
    assert q.validate(99, "x") == 99


def test_ge_rejects_below():
    with pytest.raises(ValueError, match=">= 1"):
        Query(ge=1).validate(0, "x")


def test_le_accepts_equal_and_below():
    q = Query(le=10)
    assert q.validate(10, "x") == 10
    assert q.validate(0, "x") == 0


def test_le_rejects_above():
    with pytest.raises(ValueError, match="<= 10"):
        Query(le=10).validate(11, "x")


# ── numeric: gt / lt (strict) ───────────────────────────────────────


def test_gt_rejects_equal():
    with pytest.raises(ValueError, match="> 5"):
        Query(gt=5).validate(5, "x")


def test_gt_accepts_above():
    assert Query(gt=5).validate(6, "x") == 6


def test_lt_accepts_below():
    """Regression: lt previously rejected valid below-bound values."""
    assert Query(lt=10).validate(9, "x") == 9
    assert Query(lt=10).validate(0, "x") == 0
    assert Query(lt=10).validate(-3, "x") == -3


def test_lt_rejects_equal():
    with pytest.raises(ValueError, match="< 10"):
        Query(lt=10).validate(10, "x")


def test_lt_rejects_above():
    with pytest.raises(ValueError, match="< 10"):
        Query(lt=10).validate(15, "x")


# ── string: min_length / max_length ─────────────────────────────────


def test_min_length_rejects_short():
    with pytest.raises(ValueError, match="at least 3"):
        Query(min_length=3).validate("ab", "x")


def test_min_length_accepts_exact():
    assert Query(min_length=3).validate("abc", "x") == "abc"


def test_max_length_rejects_long():
    with pytest.raises(ValueError, match="at most 5"):
        Query(max_length=5).validate("abcdef", "x")


# ── string: pattern (the) / regex (legacy) ───────────────


def test_pattern_accepts_match():
    q = Query(pattern=r"^[a-z]+$")
    assert q.validate("hello", "x") == "hello"


def test_pattern_rejects_non_match():
    with pytest.raises(ValueError, match="does not match"):
        Query(pattern=r"^[a-z]+$").validate("Hello123", "x")


def test_regex_legacy_kwarg_still_works():
    q = Query(regex=r"^\d+$")
    assert q.validate("42", "x") == "42"
    with pytest.raises(ValueError):
        q.validate("abc", "x")


def test_pattern_wins_over_regex_when_both_given():
    q = Query(regex=r"^\d+$", pattern=r"^[a-z]+$")
    # pattern (letters) is in effect, not regex (digits).
    assert q.validate("abc", "x") == "abc"
