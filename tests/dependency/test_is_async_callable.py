"""Regression tests for `_is_async_callable` detection across callable shapes."""

import functools

from veloce._internal import _is_async_callable


async def _async_fn():
    return 1


def _sync_fn():
    return 1


class AsyncCallable:
    async def __call__(self):
        return 1


class SyncCallable:
    def __call__(self):
        return 1


def test_detects_regular_async_function():
    assert _is_async_callable(_async_fn) is True


def test_detects_regular_sync_function():
    assert _is_async_callable(_sync_fn) is False


def test_detects_async_call_on_class_instance():
    # `iscoroutinefunction(instance)` is False — must inspect `__call__`.
    assert _is_async_callable(AsyncCallable()) is True


def test_detects_sync_call_on_class_instance():
    assert _is_async_callable(SyncCallable()) is False


def test_detects_functools_partial_of_async_function():
    # Python 3.12+ — `iscoroutinefunction` unwraps `functools.partial`.
    assert _is_async_callable(functools.partial(_async_fn)) is True


def test_detects_functools_partial_of_sync_function():
    assert _is_async_callable(functools.partial(_sync_fn)) is False


def test_repeated_calls_use_cache():
    # Hit the memoised path twice; both must agree.
    instance = AsyncCallable()
    assert _is_async_callable(instance) is True
    assert _is_async_callable(instance) is True


# ── Callables the weak-key memo cannot store ─────────────────────────
#
# The cache is a `WeakKeyDictionary`: `.get()` raises for an unhashable key and
# `__setitem__` raises for one that cannot be weak-referenced. Both must fall
# through to a correct (if uncached) answer rather than propagating. This is
# the same idiom `_model_backend.adapter_for` runs over `_adapters`; the two
# are kept as separate copies deliberately, so both need their own coverage.


class UnhashableAsync:
    __hash__ = None  # type: ignore[assignment]

    async def __call__(self):
        return 1


class UnhashableSync:
    __hash__ = None  # type: ignore[assignment]

    def __call__(self):
        return 1


class SlottedAsync:
    """No `__weakref__` slot, so `WeakKeyDictionary.__setitem__` rejects it."""

    __slots__ = ()

    async def __call__(self):
        return 1


def test_unhashable_async_callable_is_detected():
    assert _is_async_callable(UnhashableAsync()) is True


def test_unhashable_sync_callable_is_detected():
    assert _is_async_callable(UnhashableSync()) is False


def test_unhashable_callable_answers_the_same_every_call():
    """It cannot be cached, so every call recomputes - the answer must not drift."""
    instance = UnhashableAsync()
    assert [_is_async_callable(instance) for _ in range(3)] == [True, True, True]


def test_callable_that_cannot_be_weak_referenced_is_detected():
    """A slotted instance rejects the cache store; the answer must still land,
    and must not change when the uncached path is taken again."""
    instance = SlottedAsync()
    assert _is_async_callable(instance) is True
    assert _is_async_callable(instance) is True


def test_a_builtin_has_no_dunder_call_to_inspect():
    """`len.__call__` is a method-wrapper, not an `async def`; the probe must
    read that as sync rather than tripping over the missing code object."""
    assert _is_async_callable(len) is False
    assert _is_async_callable(len) is False


def test_a_non_callable_object_is_not_async():
    assert _is_async_callable(object()) is False


def test_lambda_is_not_async():
    assert _is_async_callable(lambda: 1) is False
