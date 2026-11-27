"""A client-requested task TTL is clamped to a server maximum.

A task-augmented `tools/call` may carry its own `ttl` in milliseconds, and the
server kept whatever it was given - its docstring said so in as many words: "the
server keeps the requested value verbatim". A settled task is reclaimed by
expiry, so the value the client picks decides how long the server holds the task
and its result:

    {"task": {"ttl": 4611686018427387904}}   ->  expires in ~146,000,000 years

which is process life. The task's result may be up to the response size limit,
so a client that calls in a loop with a large `ttl` grows the store without
bound. Owner eviction reclaims a *session's* tasks when the session goes away,
but that is the wrong lever: a long-lived connection never triggers it.

The requested value is now clamped rather than refused. A clamped call still
succeeds, and the client reads the TTL it actually got back off the task object,
so a client asking for more than it can have learns what it has.
"""

from __future__ import annotations

import time

import pytest

from veloce.contrib.mcp.tasks import (
    _DEFAULT_TASK_TTL_SECONDS,
    _MAX_TASK_TTL_SECONDS,
    _era_modern_var,
    new_task,
    task_ttl_ms,
)

_MAX_MS = _MAX_TASK_TTL_SECONDS * 1000
_DEFAULT_MS = _DEFAULT_TASK_TTL_SECONDS * 1000


def _requested(ttl):
    return task_ttl_ms({"task": {"ttl": ttl}})


# ── the clamp ────────────────────────────────────────────────────────


def test_an_enormous_ttl_is_clamped():
    """The defect: 999999999999 ms is 31.7 years, and it was honoured."""
    assert _requested(999_999_999_999) == _MAX_MS


def test_a_ttl_at_the_integer_extreme_is_clamped():
    assert _requested(2**62) == _MAX_MS


def test_a_ttl_one_millisecond_over_the_maximum_is_clamped():
    assert _requested(_MAX_MS + 1) == _MAX_MS


def test_a_clamped_task_expires_within_the_maximum():
    """The clamp has to reach the task, not just the parse."""
    task = new_task("t", _requested(2**62))
    assert task.expires_at - time.monotonic() <= _MAX_TASK_TTL_SECONDS + 1


def test_the_maximum_is_longer_than_the_default():
    """A clamp below the default would silently shorten every ordinary task."""
    assert _MAX_TASK_TTL_SECONDS > _DEFAULT_TASK_TTL_SECONDS


# ── every value a client can legitimately ask for is honoured ────────
#
# The negatives. A clamp that also shortened reasonable requests would break the
# feature it is protecting.


def test_a_ttl_exactly_at_the_maximum_is_honoured():
    assert _requested(_MAX_MS) == _MAX_MS


def test_a_ttl_below_the_maximum_is_honoured():
    assert _requested(_MAX_MS - 1) == _MAX_MS - 1


def test_a_short_ttl_is_honoured():
    assert _requested(1_000) == 1_000


def test_a_one_millisecond_ttl_is_honoured():
    assert _requested(1) == 1


def test_a_ttl_longer_than_the_default_but_under_the_maximum_is_honoured():
    """Extending past the default is the point of the field."""
    longer = _DEFAULT_MS * 2
    assert longer < _MAX_MS
    assert _requested(longer) == longer


def test_a_task_with_a_short_ttl_expires_when_asked():
    task = new_task("t", 1_000)
    assert 0 < task.expires_at - time.monotonic() <= 1.1


# ── the shapes that were already rejected still are ──────────────────


def test_no_task_field_takes_the_default():
    assert task_ttl_ms({}) == _DEFAULT_MS


def test_an_empty_task_field_takes_the_default():
    assert task_ttl_ms({"task": {}}) == _DEFAULT_MS


@pytest.mark.parametrize("ttl", [0, -1, -999, "600000", None, 1.5, True, False, [600]])
def test_a_non_positive_or_non_integer_ttl_takes_the_default(ttl):
    """`True` is an `int` in Python; the existing bool guard must survive."""
    assert task_ttl_ms({"task": {"ttl": ttl}}) == _DEFAULT_MS


def test_a_non_dict_task_field_takes_the_default():
    assert task_ttl_ms({"task": "yes"}) == _DEFAULT_MS


# ── the clamped value is what the client is told ─────────────────────


def test_the_task_reports_the_ttl_it_actually_got():
    """A client asking for more than it can have must be able to see that."""
    task = new_task("t", _requested(2**62))
    assert task.ttl_ms == _MAX_MS


def test_the_reported_ttl_appears_in_the_wire_shape():
    token = _era_modern_var.set(True)
    try:
        assert new_task("t", _requested(2**62)).describe()["ttlMs"] == _MAX_MS
    finally:
        _era_modern_var.reset(token)


def test_the_legacy_wire_shape_reports_it_too():
    token = _era_modern_var.set(False)
    try:
        assert new_task("t", _requested(2**62)).describe()["ttl"] == _MAX_MS
    finally:
        _era_modern_var.reset(token)
