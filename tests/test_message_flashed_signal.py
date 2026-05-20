"""message_flashed signal — fires on every flash() call (SI3)."""

from __future__ import annotations

from veloce import Veloce
from veloce.helpers import flash
from veloce.signals import message_flashed


def test_message_flashed_signal_exists():
    assert message_flashed is not None
    assert message_flashed.name == "message-flashed"


def test_flash_emits_message_flashed():
    app = Veloce()
    received: list[tuple] = []

    def listener(sender, message=None, category=None):
        received.append((message, category))

    message_flashed.connect(listener)
    try:
        with app.test_request_context():
            flash("Saved!", "success")
        assert received == [("Saved!", "success")]
    finally:
        message_flashed.disconnect(listener)


def test_flash_default_category_in_signal():
    app = Veloce()
    received: list[tuple] = []

    def listener(sender, message=None, category=None):
        received.append((message, category))

    message_flashed.connect(listener)
    try:
        with app.test_request_context():
            flash("plain message")
        assert received == [("plain message", "message")]
    finally:
        message_flashed.disconnect(listener)


def test_multiple_flashes_emit_multiple_signals():
    app = Veloce()
    count: list[int] = []

    def listener(sender, message=None, category=None):
        count.append(1)

    message_flashed.connect(listener)
    try:
        with app.test_request_context():
            flash("a")
            flash("b")
            flash("c")
        assert len(count) == 3
    finally:
        message_flashed.disconnect(listener)
