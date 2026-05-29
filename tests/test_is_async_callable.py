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
