"""Signals — minimal pub/sub.

Provides a lightweight `Signal` class that exposes a
`signal.connect(receiver)` / `signal.send(sender, **kwargs)` API, so
signal-based application code stays small.

Veloce ships eight standard signals. Four fire around a request:

- `request_started(sender=app, request=...)`
- `request_finished(sender=app, request=..., response=...)`
- `request_tearing_down(sender=app, exc=...)`
- `got_request_exception(sender=app, exception=...)`

three around an application context:

- `appcontext_pushed(sender=app)`
- `appcontext_popped(sender=app)`
- `appcontext_tearing_down(sender=app, exc=...)`

and one for flashed messages:

- `message_flashed(sender=app, message=..., category=...)`

This list said "four" and named the first group only, so the other four read as
undocumented internals.

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

import asyncio
import contextvars
import inspect
import logging
import sys
import weakref
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

from veloce._constants import MSG_RECEIVER_RAISED

_logger = logging.getLogger(__name__)

# Sentinel for "connect to all senders" - the public API exports it so
# callers can write `signal.connect(fn, sender=ANY_SENDER)` explicitly.
# Identity comparison only; never construct another instance.
ANY_SENDER: Any = object()

# Shared return shape for `send`, `send_robust`, and `send_robust_async`.
# Each entry is `(receiver, value)` where `value` is the receiver's
# return value - or, for the `_robust` variants, the `Exception`
# instance the receiver raised.
SignalResult = list[tuple[Callable, Any]]


# ── Matching helpers ──────────────────────────────────────


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


# ── Concurrent async dispatch ─────────────────────────────


async def _run_async_concurrently(
    coros: list[Coroutine[Any, Any, Any]],
    *,
    context: contextvars.Context,
) -> list[Any]:
    """Run `coros` concurrently, each inside a copy of `context`.

    `context` is the dispatch-time snapshot captured by the caller BEFORE
    any sync receiver ran, so async receivers observe the original
    request-local context rather than any value a sync receiver mutated.
    Results are returned positionally, matching the order of `coros`.

    Every coroutine always runs to completion - `gather` is driven with
    `return_exceptions=True`, so a failing receiver yields its `Exception`
    instance in place of a return value and never cancels the others. This
    holds for BOTH the robust and non-robust callers: the non-robust
    `asend` re-raises the first exception itself AFTER this function
    returns, so by the time it raises no receiver is still running. Using
    `return_exceptions=False` here would re-raise the first failure
    immediately while the other already-scheduled tasks keep executing in
    the background, leaking receivers past the point `asend` returns.
    """
    if not coros:
        return []
    if sys.version_info >= (3, 11):
        # `Task.__init__` gained a per-task `context=` in 3.11, so each
        # receiver can run under its own copy of the dispatch-time
        # snapshot. Drive the tasks through `gather`, which preserves
        # positional ordering and, with `return_exceptions=True`, waits
        # for every task to finish (no ExceptionGroup, no early cancel).
        loop = asyncio.get_running_loop()
        tasks = [loop.create_task(coro, context=context.copy()) for coro in coros]
        return await asyncio.gather(*tasks, return_exceptions=True)

    # 3.10 fallback: neither `asyncio.gather` nor `Task.__init__` accept a
    # per-task `context=` before 3.11. A bare `gather` would create the
    # tasks under whatever context is current now - which, after the sync
    # receivers ran, may carry their mutations. Create the tasks from
    # inside the dispatch-time snapshot so they capture it instead; the
    # only 3.10 difference from 3.11 is that the tasks share that context
    # rather than each getting an isolated copy.
    return await context.run(lambda: asyncio.gather(*coros, return_exceptions=True))


# ── Signal ────────────────────────────────────────────────


class Signal:
    """A named pub/sub signal - standard shape.

    Receivers connect via `connect(receiver, sender=ANY_SENDER)` and
    detach via `disconnect(receiver, sender=ANY_SENDER)`.
    `send(sender, **kwargs)` fires every receiver subscribed for that
    exact `sender` (compared by `is`, falling back to `==`) plus every
    receiver subscribed for `ANY_SENDER`. Return values are collected
    into a list of `(receiver, value)` tuples so callers can introspect
    what fired, though veloce's own code ignores the return value.

    `asend` and `send_robust_async` await async receivers concurrently
    (sync receivers still run inline in registration order first).
    """

    __slots__ = ("name", "doc", "_subs")

    def __init__(self, name: str = "", doc: str | None = None) -> None:
        self.name = name
        #: What this signal is for, as given to `Namespace.signal(name, doc=...)`.
        #: `None` when the signal was constructed directly without one.
        self.doc = doc
        # Each subscription: (sender, ref_or_callable, is_weak, is_async).
        # `sender` is `ANY_SENDER` for unfiltered receivers, else the
        # sender itself (strong reference - typical senders are app
        # singletons that already outlive the signal anyway). `is_async`
        # is computed once at connect time via `iscoroutinefunction`; a
        # runtime `iscoroutine(value)` fallback still catches custom
        # callables that return coroutines without being coroutine
        # functions, so the classification never regresses behavior.
        self._subs: list[tuple[Any, Any, bool, bool]] = []

    def connect(
        self,
        receiver: Callable,
        weak: bool = True,
        *,
        sender: Any = ANY_SENDER,
    ) -> Callable:
        """Register `receiver` to fire when `send(sender)` runs.

        `sender=ANY_SENDER` (the default) subscribes to every send.
        Pass a specific sender to filter - the receiver then only fires
        when `send` is called with that exact sender. Returns the
        receiver unchanged so it can be used as a decorator.
        """
        is_async = inspect.iscoroutinefunction(receiver)
        if weak:
            try:
                ref: Any = weakref.WeakMethod(receiver)
            except TypeError:
                ref = weakref.ref(receiver)
            self._subs.append((sender, ref, True, is_async))
        else:
            self._subs.append((sender, receiver, False, is_async))
        return receiver

    def disconnect(self, receiver: Callable, *, sender: Any = ANY_SENDER) -> None:
        """Remove the subscription for `(receiver, sender)`.

        Mirrors `connect` - to detach a per-sender subscription pass the
        same `sender`. With the default `sender=ANY_SENDER` it removes
        any subscription matching `receiver`, regardless of which sender
        it was bound to (back-compat with the previous unfiltered API).

        Targeted detach matches the **stored** `sender` directly, not via
        `_matches` - `_matches` is the `send`-time rule ("does this
        subscription fire for that sender?"), where a stored
        `ANY_SENDER` deliberately matches every send. Reusing that rule
        in `disconnect` would silently delete an `ANY_SENDER`
        subscription whenever the caller targeted a specific sender.
        """
        for i, (sub_sender, ref, is_weak, _is_async) in enumerate(self._subs):
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

    def _iter_live_pairs(self, sender: Any) -> Iterator[tuple[Callable, bool]]:
        """Yield `(target, is_async)` for live matching receivers; prune dead refs.

        Walks `self._subs` once, resolves weakrefs, drops dead entries,
        and yields the resolved target plus its connect-time `is_async`
        flag for each entry whose stored sender matches `sender` via
        `_matches`. After iteration the subscription list contains no
        dead refs.
        """
        live: list[tuple[Any, Any, bool, bool]] = []
        for sub_sender, ref, is_weak, is_async in self._subs:
            target = ref() if is_weak else ref
            if target is None:  # dead weakref - drop on the next pass
                continue
            live.append((sub_sender, ref, is_weak, is_async))
            if _matches(sub_sender, sender):
                yield target, is_async
        if len(live) != len(self._subs):
            self._subs = live

    def _iter_live_targets(self, sender: Any) -> Iterator[Callable]:
        """Yield live receivers that match `sender`; prune dead weakrefs in place.

        Thin wrapper over `_iter_live_pairs` that discards the `is_async`
        flag, so the sync `send`/`send_robust` paths stay unchanged.
        """
        for target, _is_async in self._iter_live_pairs(sender):
            yield target

    def send(self, sender: Any = None, **kwargs: Any) -> SignalResult:
        """Fire receivers subscribed for `sender` (and for ANY_SENDER).

        Returns `(receiver, value)` pairs in registration order. With no
        subscriptions the call short-circuits, so callers can invoke
        `send` unconditionally rather than guarding with
        `has_receivers_for` - a single live-scan then both fires and
        prunes dead weakrefs.
        """
        if not self._subs:
            return []
        return [(target, target(sender, **kwargs)) for target in self._iter_live_targets(sender)]

    def send_robust(self, sender: Any = None, **kwargs: Any) -> SignalResult:
        """Like `send`, but never aborts on a failing receiver.

        Returns `(receiver, value)` pairs in registration order. The
        second tuple element is the receiver's return value, OR an
        `Exception` instance if the receiver raised. Per-receiver
        exceptions are logged at WARNING and substituted into the
        result list so the caller can inspect failures while subsequent
        receivers still fire.

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
                    MSG_RECEIVER_RAISED,
                    getattr(target, "__qualname__", repr(target)),
                    self.name,
                    exc.__class__.__name__,
                    exc_info=True,
                )
                value = exc
            else:
                if inspect.iscoroutine(value):
                    # Async receiver with sync send - close coroutine to suppress
                    # the "coroutine was never awaited" RuntimeWarning and surface
                    # the misuse as a TypeError result entry.
                    value.close()
                    name = getattr(target, "__qualname__", repr(target))
                    value = TypeError(
                        "async receiver requires Signal.send_robust_async; "
                        f"got coroutine from {name!r}"
                    )
                    _logger.warning(
                        "Receiver %r for signal %r returned a coroutine; "
                        "use send_robust_async to await async receivers",
                        name,
                        self.name,
                    )
            results.append((target, value))
        return results

    async def asend(self, sender: Any = None, **kwargs: Any) -> SignalResult:
        """Async, non-robust send - awaits async receivers concurrently.

        Returns `(receiver, value)` pairs in registration order. Sync
        receivers run inline immediately, preserving registration order
        and raising on the first sync error exactly like `send`. Async
        receivers are collected and awaited concurrently, each inside a
        copy of the dispatch-time context. Like `send`, the first failing
        receiver propagates its exception (non-robust contract); use
        `send_robust_async` to capture per-receiver failures instead.

        Even on the non-robust path every async receiver runs to
        completion before this coroutine returns OR raises: the concurrent
        run collects all results (failures included), and only afterwards
        is the first exception, in receiver order, re-raised. This
        guarantees no receiver is still touching request-scoped state once
        `asend` has returned - a `return_exceptions=False` gather would
        instead re-raise the first failure while later receivers kept
        running in the background past teardown.
        """
        # No subscriptions: skip the context snapshot and scaffolding, exactly
        # as `send` does, so callers can dispatch unconditionally.
        if not self._subs:
            return []
        # Snapshot the dispatch-time context BEFORE any sync receiver runs.
        # Sync receivers run inline below and may mutate ContextVars; async
        # receivers must still observe the caller's original request-local
        # context, so they run under this pre-mutation snapshot.
        dispatch_context = contextvars.copy_context()
        targets: list[tuple[Callable, Any, bool]] = []
        coros: list[Coroutine[Any, Any, Any]] = []
        coro_slots: list[int] = []
        for target, is_async in self._iter_live_pairs(sender):
            value: Any = target(sender, **kwargs)
            if is_async or inspect.iscoroutine(value):
                # Defer awaiting so async receivers run concurrently.
                coro_slots.append(len(targets))
                coros.append(value)
                targets.append((target, None, True))
            else:
                targets.append((target, value, False))
        awaited = await _run_async_concurrently(coros, context=dispatch_context)
        # All async receivers have now finished. Fill in their results, and
        # remember the first one (in receiver/registration order) that raised
        # so it can be re-raised below - after every task has completed.
        first_exc: BaseException | None = None
        for slot, result in zip(coro_slots, awaited, strict=True):
            target, _, _ = targets[slot]
            targets[slot] = (target, result, True)
            if first_exc is None and isinstance(result, BaseException):
                first_exc = result
        if first_exc is not None:
            raise first_exc
        return [(target, value) for target, value, _ in targets]

    async def send_robust_async(self, sender: Any = None, **kwargs: Any) -> SignalResult:
        """Async variant of `send_robust` - awaits async receivers concurrently.

        Returns `(receiver, value)` pairs in registration order. The
        second tuple element is the receiver's return value, OR an
        `Exception` instance if the receiver raised. Sync receivers run
        inline first, in registration order, each wrapped so a raised
        exception is recorded as its result. Async receivers (or any
        receiver returning a coroutine) are then awaited concurrently;
        one failing receiver never cancels the others. Per-receiver
        exceptions, raised either at call time or while awaiting, are
        logged at WARNING and substituted into the result list.
        """
        # No subscriptions: skip the context snapshot and scaffolding, exactly
        # as `send` does, so callers can dispatch unconditionally.
        if not self._subs:
            return []
        # Snapshot the dispatch-time context BEFORE any sync receiver runs, so
        # async receivers observe the caller's original context even if a sync
        # receiver mutated a ContextVar (see `asend`).
        dispatch_context = contextvars.copy_context()
        targets: list[tuple[Callable, Any]] = []
        coros: list[Coroutine[Any, Any, Any]] = []
        coro_slots: list[int] = []
        for target, is_async in self._iter_live_pairs(sender):
            try:
                value: Any = target(sender, **kwargs)
            except Exception as exc:
                self._log_receiver_raised(target, exc)
                targets.append((target, exc))
                continue
            if is_async or inspect.iscoroutine(value):
                coro_slots.append(len(targets))
                coros.append(value)
                targets.append((target, None))
            else:
                targets.append((target, value))
        awaited = await _run_async_concurrently(coros, context=dispatch_context)
        for slot, result in zip(coro_slots, awaited, strict=True):
            target = targets[slot][0]
            if isinstance(result, Exception):
                self._log_receiver_raised(target, result)
            targets[slot] = (target, result)
        return targets

    def has_receivers_for(self, sender: Any = None) -> bool:
        """`True` if any connected receiver would fire for `sender`.

        A side-effect-free predicate that short-circuits on the first
        live, matching receiver. Dead weakrefs are skipped but not pruned
        here; pruning is left to `send` / `_iter_live_targets`.
        """
        for sub_sender, ref, is_weak, _is_async in self._subs:
            target = ref() if is_weak else ref
            if target is None:  # dead weakref - skip without firing
                continue
            if _matches(sub_sender, sender):
                return True
        return False

    def _log_receiver_raised(self, target: Callable, exc: BaseException) -> None:
        """Log a receiver failure at WARNING with the traceback attached."""
        _logger.warning(
            MSG_RECEIVER_RAISED,
            getattr(target, "__qualname__", repr(target)),
            self.name,
            exc.__class__.__name__,
            exc_info=exc,
        )

    def __repr__(self) -> str:
        return f"<Signal name={self.name!r} receivers={len(self._subs)}>"


# ── Namespace ─────────────────────────────────────────────


class Namespace:
    """A factory that returns named `Signal` instances, one per name.

    Calling `signal(name)` repeatedly with the same name returns the
    same `Signal` object, so independent parts of an application can
    obtain a shared signal by agreeing on a name rather than passing the
    instance around.

    Usage::

        from veloce.signals import Namespace

        signals = Namespace()
        user_registered = signals.signal("user-registered")

        @user_registered.connect
        def welcome(sender, **kw):
            ...

        user_registered.send(app, user=user)
    """

    __slots__ = ("_signals",)

    def __init__(self) -> None:
        self._signals: dict[str, Signal] = {}

    def signal(self, name: str, doc: str | None = None) -> Signal:
        """Return the `Signal` named `name`, creating it on first use.

        Repeated calls with the same `name` return the identical instance, and
        the first call's `doc` is the one kept - a later call naming a different
        one does not rewrite a signal other code already holds.

        `doc` is recorded on the signal as `Signal.doc`. It used to be accepted
        and discarded, which made it a silent no-op for anyone porting code that
        passed it.
        """
        existing = self._signals.get(name)
        if existing is not None:
            return existing
        sig = Signal(name, doc)
        self._signals[name] = sig
        return sig

    def __repr__(self) -> str:
        return f"<Namespace signals={len(self._signals)}>"


# ── Standard signals ──────────────────────────────────────


# Module-level singletons - apps subscribe via `request_started.connect(fn)`.
request_started = Signal("request-started")
request_finished = Signal("request-finished")
request_tearing_down = Signal("request-tearing-down")
got_request_exception = Signal("got-request-exception")
# Fires for every `flash()` call - `flash(sender=app, message=..., category=...)`.
message_flashed = Signal("message-flashed")
# App-context lifecycle - fire on `app.app_context()` enter/exit.
appcontext_pushed = Signal("appcontext-pushed")
appcontext_popped = Signal("appcontext-popped")
appcontext_tearing_down = Signal("appcontext-tearing-down")
