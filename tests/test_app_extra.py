"""Veloce(**extra) constructor passthrough — app.extra (CF12)."""

from __future__ import annotations

from veloce import Veloce


def test_extra_empty_by_default():
    assert Veloce().extra == {}


def test_extra_captures_unknown_kwargs():
    app = Veloce(team="payments", tier="gold")
    assert app.extra == {"team": "payments", "tier": "gold"}


def test_extra_does_not_swallow_known_kwargs():
    app = Veloce(title="My API", custom_flag=True)
    # `title` is a real param; only the unknown one lands in `extra`.
    assert app.title == "My API"
    assert app.extra == {"custom_flag": True}


def test_extra_is_a_plain_dict():
    app = Veloce(x=1)
    assert isinstance(app.extra, dict)
    app.extra["y"] = 2
    assert app.extra == {"x": 1, "y": 2}


def test_extra_isolated_per_app():
    a = Veloce(marker="a")
    b = Veloce(marker="b")
    assert a.extra == {"marker": "a"}
    assert b.extra == {"marker": "b"}
