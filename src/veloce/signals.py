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


class Signal:
    """A named pub/sub signal — standard shape.

    Receivers connect via `connect(receiver, sender=ANY)` and detach
    via `disconnect(receiver, sender=ANY)`. `send(sender, **kwargs)`
    fires every connected receiver, passing `sender` positionally and
    forwarding `kwargs`. Return values are collected into a list of
    `(receiver, value)` tuples so callers can introspect what fired,
    though veloce's own code ignores the return value.
    """

    __slots__ = ("name", "_receivers", "_strong")

    def __init__(self, name: str = "") -> None:
        self.name = name
        # Two parallel slots for weak vs strong refs. Strong refs go
        # into `_strong` (a plain list); weak refs into `_receivers` (a
        # list of weakref objects). Lookup walks both.
        self._receivers: list[Any] = []
        self._strong: list[Callable] = []

    def connect(self, receiver: Callable, weak: bool = True) -> Callable:
        """Register `receiver` to fire on `send`. Returns the receiver
        unchanged so it can be used as a decorator."""
        if not weak:
            self._strong.append(receiver)
            return receiver

        try:
            ref: Any = weakref.WeakMethod(receiver)
        except TypeError:
            ref = weakref.ref(receiver)
        self._receivers.append(ref)
        return receiver

    def disconnect(self, receiver: Callable) -> None:
        """Remove `receiver` from the receiver set (no-op if absent)."""
        # Strong refs first — direct identity compare.
        try:
            self._strong.remove(receiver)
            return
        except ValueError:
            pass
        # Walk weak refs and drop the one whose target is `receiver`.
        for i, ref in enumerate(self._receivers):
            try:
                target = ref()
            except Exception:
                target = None
            if target is receiver:
                del self._receivers[i]
                return

    def send(self, sender: Any = None, **kwargs: Any) -> list[tuple[Callable, Any]]:
        """Fire every connected receiver. Returns `(receiver, value)`
        pairs in registration order (strong refs first, then weak)."""
        results: list[tuple[Callable, Any]] = []
        for fn in list(self._strong):
            results.append((fn, fn(sender, **kwargs)))
        live: list[Any] = []
        for ref in self._receivers:
            try:
                target = ref()
            except Exception:
                target = None
            if target is None:
                continue
            live.append(ref)
            results.append((target, target(sender, **kwargs)))
        # Purge dead refs lazily.
        if len(live) != len(self._receivers):
            self._receivers = live
        return results

    def has_receivers_for(self, sender: Any = None) -> bool:
        """`True` if any connected receiver would fire for `sender`."""
        _ = sender  # sender-specific filtering not yet implemented
        if self._strong:
            return True
        for ref in self._receivers:
            try:
                if ref() is not None:
                    return True
            except Exception:
                pass
        return False

    def __repr__(self) -> str:
        n_recvs = len(self._strong) + len(self._receivers)
        return f"<Signal name={self.name!r} receivers={n_recvs}>"


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
