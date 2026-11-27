"""The signals module documents every signal it ships.

Its docstring said "Veloce ships four standard signals" and listed four. The
module defines **eight** module-level `Signal` singletons - the four
request-lifecycle ones it named, three application-context ones, and
`message_flashed` - so half the public signal surface read as an undocumented
internal.

A count in prose is the kind of thing that is right when written and wrong two
releases later, so it is asserted here rather than trusted.
"""

from __future__ import annotations

import pytest

from veloce import signals

SHIPPED = sorted(
    name
    for name, value in vars(signals).items()
    if isinstance(value, signals.Signal) and not name.startswith("_")
)


def test_the_module_ships_eight_signals():
    assert len(SHIPPED) == 8, SHIPPED


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_signal_is_documented(name):
    """The defect: four of the eight were not."""
    assert f"`{name}(" in (signals.__doc__ or ""), name


def test_the_docstring_does_not_undercount():
    doc = signals.__doc__ or ""
    assert "ships four standard signals" not in doc
    assert "eight standard signals" in doc


def test_the_scan_finds_real_signals():
    """Vacuity guard: an empty list would pass the per-signal check."""
    assert "request_started" in SHIPPED
    assert "message_flashed" in SHIPPED
    assert "appcontext_pushed" in SHIPPED
