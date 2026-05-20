"""appcontext_pushed / popped / tearing_down signals (SI2)."""

from __future__ import annotations

from veloce import Veloce
from veloce.signals import (
    appcontext_popped,
    appcontext_pushed,
    appcontext_tearing_down,
)


def test_appcontext_signals_exist():
    assert appcontext_pushed.name == "appcontext-pushed"
    assert appcontext_popped.name == "appcontext-popped"
    assert appcontext_tearing_down.name == "appcontext-tearing-down"


def test_pushed_and_popped_fire_in_order():
    app = Veloce()
    events: list[str] = []

    def on_push(sender, **kw):
        events.append("push")

    def on_pop(sender, **kw):
        events.append("pop")

    appcontext_pushed.connect(on_push)
    appcontext_popped.connect(on_pop)
    try:
        with app.app_context():
            assert events == ["push"]
        assert events == ["push", "pop"]
    finally:
        appcontext_pushed.disconnect(on_push)
        appcontext_popped.disconnect(on_pop)


def test_tearing_down_fires_before_pop():
    app = Veloce()
    events: list[str] = []

    def on_teardown(sender, **kw):
        events.append("teardown")

    def on_pop(sender, **kw):
        events.append("pop")

    appcontext_tearing_down.connect(on_teardown)
    appcontext_popped.connect(on_pop)
    try:
        with app.app_context():
            pass
        assert events == ["teardown", "pop"]
    finally:
        appcontext_tearing_down.disconnect(on_teardown)
        appcontext_popped.disconnect(on_pop)


def test_signals_carry_the_app_as_sender():
    app = Veloce()
    senders: list = []

    def listener(sender, **kw):
        senders.append(sender)

    appcontext_pushed.connect(listener)
    try:
        with app.app_context():
            pass
        assert senders == [app]
    finally:
        appcontext_pushed.disconnect(listener)
