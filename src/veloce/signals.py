"""Signals — minimal pub/sub.

Provides a lightweight `Signal` class that exposing a
`signal.connect(receiver)` / `signal.send(sender, **kwargs)` API, so
signal-based application code stays small.

Veloce ships four standard signals:

- `request_started(sender=app, request=...)`
- `request_finished(sender=app, request=..., response=...)`
- `request_tearing_down(sender=app, exc=...)`
- `got_request_exception(sender=app, exception=...)`

Receivers are stored as weakrefs by default so handlers don't pin
their owners alive. Pass `weak=False` to keep a strong reference
(useful for module-level functions that are anchored elsewhere).

Implementation: weak-ref bookkeeping uses `weakref.WeakMethod` for
bound methods and `weakref.ref` for plain functions. Dead refs are
purged lazily on the next `send`.

The implementation is deliberately small and self-contained. Keeping
the surface minimal lets application code stay decoupled from the
signal plumbing.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from typing import Any

# Sentinel for "connect to all senders" — the public API exports it so
# callers can write `signal.connect(fn, sender=ANY_SENDER)` explicitly.
# Identity comparison only; never construct another instance.
ANY_SENDER: Any = object()


def _matches(subscribed: Any, sent: Any) -> bool:
    """A receiver subscribed for `subscribed` should fire on `sent`."""
    if subscribed is ANY_SENDER:
        return True
    if subscribed is sent:
        return True
    # Fall back to equality so primitive senders ("login", 1, ...) work
    # the way callers expect; guard against types whose `__eq__` raises.
    try:
        return bool(subscribed == sent)
    except Exception:
        return False


class Signal:
    """A named pub/sub signal — standard shape.

    Receivers connect via `connect(receiver, sender=ANY_SENDER)` and
    detach via `disconnect(receiver, sender=ANY_SENDER)`.
    `send(sender, **kwargs)` fires every receiver subscribed for that
    exact `sender` (compared by `is`, falling back to `==`) plus every
    receiver subscribed for `ANY_SENDER`. Return values are collected
    into a list of `(receiver, value)` tuples so callers can introspect
    what fired, though veloce's own code ignores the return value.
    """

    __slots__ = ("name", "_subs")

    def __init__(self, name: str = "") -> None:
        self.name = name
        # Each subscription: (sender, ref_or_callable, is_weak). `sender`
        # is `ANY_SENDER` for unfiltered receivers, else the sender
        # itself (strong reference — typical senders are app singletons
        # that already outlive the signal anyway).
        self._subs: list[tuple[Any, Any, bool]] = []

    def connect(
        self,
        receiver: Callable,
        weak: bool = True,
        *,
        sender: Any = ANY_SENDER,
    ) -> Callable:
        """Register `receiver` to fire when `send(sender)` runs.

        `sender=ANY_SENDER` (the default) subscribes to every send.
        Pass a specific sender to filter — the receiver then only fires
        when `send` is called with that exact sender. Returns the
        receiver unchanged so it can be used as a decorator.
        """
        if weak:
            try:
                ref: Any = weakref.WeakMethod(receiver)
            except TypeError:
                ref = weakref.ref(receiver)
            self._subs.append((sender, ref, True))
        else:
            self._subs.append((sender, receiver, False))
        return receiver

    def disconnect(self, receiver: Callable, *, sender: Any = ANY_SENDER) -> None:
        """Remove the subscription for `(receiver, sender)`.

        Mirrors `connect` — to detach a per-sender subscription pass the
        same `sender`. With the default `sender=ANY_SENDER` it removes
        any subscription matching `receiver`, regardless of which sender
        it was bound to (back-compat with the previous unfiltered API).
        """
        for i, (sub_sender, ref, is_weak) in enumerate(self._subs):
            target = ref() if is_weak else ref
            if target is not receiver:
                continue
            if sender is ANY_SENDER or _matches(sub_sender, sender):
                del self._subs[i]
                return

    def send(self, sender: Any = None, **kwargs: Any) -> list[tuple[Callable, Any]]:
        """Fire receivers subscribed for `sender` (and for ANY_SENDER).

        Returns `(receiver, value)` pairs in registration order.
        """
        results: list[tuple[Callable, Any]] = []
        live: list[tuple[Any, Any, bool]] = []
        for sub_sender, ref, is_weak in self._subs:
            target = ref() if is_weak else ref
            if target is None:  # dead weakref — drop on the next pass
                continue
            live.append((sub_sender, ref, is_weak))
            if _matches(sub_sender, sender):
                results.append((target, target(sender, **kwargs)))
        if len(live) != len(self._subs):
            self._subs = live
        return results

    def has_receivers_for(self, sender: Any = None) -> bool:
        """`True` if any connected receiver would fire for `sender`."""
        for sub_sender, ref, is_weak in self._subs:
            target = ref() if is_weak else ref
            if target is None:
                continue
            if _matches(sub_sender, sender):
                return True
        return False

    def __repr__(self) -> str:
        return f"<Signal name={self.name!r} receivers={len(self._subs)}>"


# ── Standard signals ────────────────────────────────────────────────


# Module-level singletons — apps subscribe via `request_started.connect(fn)`.
request_started = Signal("request-started")
request_finished = Signal("request-finished")
request_tearing_down = Signal("request-tearing-down")
got_request_exception = Signal("got-request-exception")
# Fires for every `flash()` call — `flash(sender=app, message=..., category=...)`.
message_flashed = Signal("message-flashed")
# App-context lifecycle — fire on `app.app_context()` enter/exit.
appcontext_pushed = Signal("appcontext-pushed")
appcontext_popped = Signal("appcontext-popped")
appcontext_tearing_down = Signal("appcontext-tearing-down")
