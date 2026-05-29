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

import inspect
import logging
import weakref
from collections.abc import Callable, Iterator
from typing import Any

_logger = logging.getLogger("veloce.signals")

# Sentinel for "connect to all senders" — the public API exports it so
# callers can write `signal.connect(fn, sender=ANY_SENDER)` explicitly.
# Identity comparison only; never construct another instance.
ANY_SENDER: Any = object()

# Shared return shape for `send`, `send_robust`, and `send_robust_async`.
# Each entry is `(receiver, value)` where `value` is the receiver's
# return value — or, for the `_robust` variants, the `Exception`
# instance the receiver raised.
SignalResult = list[tuple[Callable, Any]]


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

        Targeted detach matches the **stored** `sender` directly, not via
        `_matches` — `_matches` is the `send`-time rule ("does this
        subscription fire for that sender?"), where a stored
        `ANY_SENDER` deliberately matches every send. Reusing that rule
        in `disconnect` would silently delete an `ANY_SENDER`
        subscription whenever the caller targeted a specific sender.
        """
        for i, (sub_sender, ref, is_weak) in enumerate(self._subs):
            target = ref() if is_weak else ref
            if target != receiver:
                continue
            if sender is ANY_SENDER:
                # "Detach any subscription for this receiver."
                del self._subs[i]
                return
            # Targeted detach: match the stored sender directly.
            if sub_sender is sender:
                del self._subs[i]
                return
            try:
                if sub_sender == sender:
                    del self._subs[i]
                    return
            except Exception:
                continue

    def _iter_live_targets(self, sender: Any) -> Iterator[Callable]:
        """Yield live receivers that match `sender`; prune dead weakrefs in place.

        Walks `self._subs` once, resolves weakrefs, drops dead entries,
        and yields the resolved target for each entry whose stored
        sender matches `sender` via `_matches`. After iteration the
        subscription list contains no dead refs.
        """
        live: list[tuple[Any, Any, bool]] = []
        for sub_sender, ref, is_weak in self._subs:
            target = ref() if is_weak else ref
            if target is None:  # dead weakref — drop on the next pass
                continue
            live.append((sub_sender, ref, is_weak))
            if _matches(sub_sender, sender):
                yield target
        if len(live) != len(self._subs):
            self._subs = live

    def send(self, sender: Any = None, **kwargs: Any) -> SignalResult:
        """Fire receivers subscribed for `sender` (and for ANY_SENDER).

        Returns `(receiver, value)` pairs in registration order.
        """
        return [(target, target(sender, **kwargs)) for target in self._iter_live_targets(sender)]

    def send_robust(self, sender: Any = None, **kwargs: Any) -> SignalResult:
        """Like `send`, but never aborts on a failing receiver.

        Returns `(receiver, value)` pairs in registration order. The
        second tuple element is the receiver's return value, OR an
        `Exception` instance if the receiver raised. Per-receiver
        exceptions are logged at WARNING and substituted into the
        result list so the caller can inspect failures while subsequent
        receivers still fire. Mirrors Django/Blinker `send_robust`.

        Sync-only: if a receiver is an async function (or otherwise
        returns a coroutine), the coroutine is closed and a `TypeError`
        is recorded in the result list instead. Use `send_robust_async`
        to await async receivers.
        """
        results: SignalResult = []
        for target in self._iter_live_targets(sender):
            try:
                value: Any = target(sender, **kwargs)
            except Exception as exc:
                _logger.warning(
                    "Receiver %r for signal %r raised %s",
                    getattr(target, "__qualname__", repr(target)),
                    self.name,
                    exc.__class__.__name__,
                    exc_info=True,
                )
                value = exc
            else:
                if inspect.iscoroutine(value):
                    # Async receiver with sync send — close coroutine to suppress
                    # the "coroutine was never awaited" RuntimeWarning and surface
                    # the misuse as a TypeError result entry.
                    value.close()
                    value = TypeError(
                        "async receiver requires Signal.send_robust_async; "
                        f"got coroutine from {getattr(target, '__qualname__', repr(target))!r}"
                    )
                    _logger.warning(
                        "Receiver %r for signal %r returned a coroutine; "
                        "use send_robust_async to await async receivers",
                        getattr(target, "__qualname__", repr(target)),
                        self.name,
                    )
            results.append((target, value))
        return results

    async def send_robust_async(self, sender: Any = None, **kwargs: Any) -> SignalResult:
        """Async variant of `send_robust` — awaits coroutine-returning receivers.

        Returns `(receiver, value)` pairs in registration order. The
        second tuple element is the receiver's return value, OR an
        `Exception` instance if the receiver raised. Sync receivers are
        called directly; async receivers (or any receiver returning a
        coroutine) are awaited. Per-receiver exceptions, raised either
        at call time or while awaiting, are logged at WARNING and
        substituted into the result list so subsequent receivers still
        fire.
        """
        results: SignalResult = []
        for target in self._iter_live_targets(sender):
            try:
                value: Any = target(sender, **kwargs)
                if inspect.iscoroutine(value):
                    value = await value
            except Exception as exc:
                _logger.warning(
                    "Receiver %r for signal %r raised %s",
                    getattr(target, "__qualname__", repr(target)),
                    self.name,
                    exc.__class__.__name__,
                    exc_info=True,
                )
                value = exc
            results.append((target, value))
        return results

    def has_receivers_for(self, sender: Any = None) -> bool:
        """`True` if any connected receiver would fire for `sender`."""
        subs = self._subs
        if not subs:
            return False
        for sub_sender, ref, is_weak in subs:
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
