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

Receivers are stored as weakrefs by default so handlers don't pin their owners
alive; pass `weak=False` to keep a strong reference. Dead refs are purged lazily
on the next `send`.
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
from veloce._internal import T_route

_logger = logging.getLogger(__name__)

# Sentinel for "connect to all senders", compared by identity only.
ANY_SENDER: Any = object()

# `(receiver, value)` per entry; for the `_robust` variants `value` may be the
# `Exception` the receiver raised.
SignalResult = list[tuple[Callable[..., Any], Any]]


# ── Matching helpers ──────────────────────────────────────


def _matches(subscribed: Any, sent: Any) -> bool:
    """True when a receiver subscribed for `subscribed` should fire on `sent`."""
    if subscribed is ANY_SENDER:
        return True
    if subscribed is sent:
        return True
    # Equality so a primitive sender ("login", 1) works; `__eq__` may raise.
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

    `context` is the snapshot the caller took BEFORE any sync receiver ran, so
    async receivers observe the original request-local context. Results come
    back positionally.

    Every coroutine runs to completion: `return_exceptions=True` yields a
    failing receiver's `Exception` in place of its return value and never
    cancels the others, so no receiver is still running once the non-robust
    `asend` re-raises.
    """
    if not coros:
        return []
    if sys.version_info >= (3, 11):
        # 3.11's per-task `context=` gives each receiver its own copy.
        loop = asyncio.get_running_loop()
        tasks = [loop.create_task(coro, context=context.copy()) for coro in coros]
        return await asyncio.gather(*tasks, return_exceptions=True)

    # 3.10 has no per-task `context=`, so the tasks share this one instead.
    return await context.run(lambda: asyncio.gather(*coros, return_exceptions=True))


# ── Signal ────────────────────────────────────────────────


class Signal:
    """A named pub/sub signal - standard shape.

    `send(sender, **kwargs)` fires every receiver subscribed for that exact
    `sender` (compared by `is`, falling back to `==`) plus every receiver
    subscribed for `ANY_SENDER`, and returns `(receiver, value)` pairs.

    `asend` and `send_robust_async` await async receivers concurrently, with
    sync receivers still running inline in registration order first.
    """

    __slots__ = ("name", "doc", "_subs")

    def __init__(self, name: str = "", doc: str | None = None) -> None:
        self.name = name
        #: As given to `Namespace.signal(name, doc=...)`; `None` without one.
        self.doc = doc
        # (sender, ref_or_callable, is_weak, is_async). `sender` is held
        # strongly, and `is_async` is classified once at connect time.
        self._subs: list[tuple[Any, Any, bool, bool]] = []

    def connect(
        self,
        receiver: T_route,
        weak: bool = True,
        *,
        sender: Any = ANY_SENDER,
    ) -> T_route:
        """Register `receiver` to fire when `send(sender)` runs.

        `sender=ANY_SENDER` (the default) subscribes to every send; a specific
        sender filters to it. Returns the receiver unchanged, so it can be used
        as a decorator.
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

    def disconnect(self, receiver: Callable[..., Any], *, sender: Any = ANY_SENDER) -> None:
        """Remove the subscription for `(receiver, sender)`.

        Mirrors `connect`; the default `sender=ANY_SENDER` removes any
        subscription matching `receiver` whatever sender it was bound to.

        A targeted detach matches the **stored** `sender` directly rather than
        through `_matches`, which is the `send`-time rule under which a stored
        `ANY_SENDER` matches every send - reusing it here would delete an
        `ANY_SENDER` subscription whenever the caller targeted one sender.
        """
        for i, (sub_sender, ref, is_weak, _is_async) in enumerate(self._subs):
            target = ref() if is_weak else ref
            if target != receiver:
                continue
            if sender is ANY_SENDER:
                # Whatever sender it was registered against.
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

    def send(self, sender: Any = None, **kwargs: Any) -> SignalResult:
        """Fire receivers subscribed for `sender` (and for ANY_SENDER).

        Returns `(receiver, value)` pairs in registration order, and
        short-circuits with no subscriptions so callers need not guard with
        `has_receivers_for`. The single live-scan also prunes dead weakrefs.
        """
        if not self._subs:
            return []
        return [(target, target(sender, **kwargs)) for target in self._iter_live_targets(sender)]

    def send_robust(self, sender: Any = None, **kwargs: Any) -> SignalResult:
        """Like `send`, but never aborts on a failing receiver.

        Returns `(receiver, value)` pairs in registration order, where `value`
        is the return value or the `Exception` the receiver raised; the
        exception is also logged at WARNING.

        Sync-only: a receiver returning a coroutine has it closed and a
        `TypeError` recorded instead. Use `send_robust_async` for async ones.
        """
        results: SignalResult = []
        for target in self._iter_live_targets(sender):
            try:
                value: Any = target(sender, **kwargs)
            except Exception as exc:
                self._log_receiver_raised(target, exc)
                value = exc
            else:
                if inspect.iscoroutine(value):
                    # Closed to suppress "coroutine was never awaited".
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

        Returns `(receiver, value)` pairs in registration order. Sync receivers
        run inline and raise on the first error exactly like `send`; async ones
        are awaited concurrently, each inside a copy of the dispatch-time
        context. Use `send_robust_async` to capture per-receiver failures.

        Every async receiver still runs to completion before this coroutine
        returns or raises - the first exception, in receiver order, is re-raised
        only once every result is in - so no receiver is still touching
        request-scoped state afterwards.
        """
        # Short-circuits as `send` does, so callers can dispatch unconditionally.
        if not self._subs:
            return []
        # Taken BEFORE the sync receivers, which may mutate ContextVars.
        dispatch_context = contextvars.copy_context()
        targets: list[tuple[Callable[..., Any], Any, bool]] = []
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
        # Every task has finished, so a raised result can now be re-raised.
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

        Returns `(receiver, value)` pairs in registration order, where `value`
        is the return value or the `Exception` the receiver raised, at call time
        or while awaiting; either is also logged at WARNING. One failing
        receiver never cancels the others.
        """
        # Short-circuits as `send` does, so callers can dispatch unconditionally.
        if not self._subs:
            return []
        # Taken before the sync receivers run, as in `asend`.
        dispatch_context = contextvars.copy_context()
        targets: list[tuple[Callable[..., Any], Any]] = []
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

    def receiver_count(self) -> int:
        """How many receivers this signal holds, live or not yet pruned.

        Counts what is stored, so a weak reference whose target was collected is
        still counted until `send` prunes it. `has_receivers_for` answers the
        *live* question instead.
        """
        return len(self._subs)

    def has_receivers_for(self, sender: Any = None) -> bool:
        """`True` if any connected receiver would fire for `sender`.

        Side-effect-free: dead weakrefs are skipped but not pruned here.
        """
        for sub_sender, ref, is_weak, _is_async in self._subs:
            target = ref() if is_weak else ref
            if target is None:  # dead weakref - skip without firing
                continue
            if _matches(sub_sender, sender):
                return True
        return False

    def _iter_live_pairs(self, sender: Any) -> Iterator[tuple[Callable[..., Any], bool]]:
        """Yield `(target, is_async)` for live matching receivers; prune dead refs.

        One walk of `self._subs`, after which the subscription list holds no
        dead refs. `is_async` is the flag recorded at connect time.
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

    def _iter_live_targets(self, sender: Any) -> Iterator[Callable[..., Any]]:
        """Yield live receivers that match `sender`; prune dead weakrefs in place."""
        for target, _is_async in self._iter_live_pairs(sender):
            yield target

    def _log_receiver_raised(self, target: Callable[..., Any], exc: BaseException) -> None:
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

    Calling `signal(name)` repeatedly returns the same `Signal` object, so
    independent parts of an application can share one by agreeing on a name.

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

        Repeated calls return the identical instance and keep the first call's
        `doc`, which is recorded as `Signal.doc` rather than discarded.
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


# Singletons - apps subscribe via `request_started.connect(fn)`.
request_started = Signal("request-started")
request_finished = Signal("request-finished")
request_tearing_down = Signal("request-tearing-down")
got_request_exception = Signal("got-request-exception")
# Fires for every `flash()` call.
message_flashed = Signal("message-flashed")
# Fire on `app.app_context()` enter/exit.
appcontext_pushed = Signal("appcontext-pushed")
appcontext_popped = Signal("appcontext-popped")
appcontext_tearing_down = Signal("appcontext-tearing-down")
