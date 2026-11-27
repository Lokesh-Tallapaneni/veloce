"""app.ensure_sync."""

from __future__ import annotations

import pytest

from veloce import Veloce


def test_ensure_sync_passes_through_sync_function():
    app = Veloce()

    def add(a, b):
        return a + b

    wrapped = app.ensure_sync(add)
    # Unchanged — sync funcs need no wrapping.
    assert wrapped is add
    assert wrapped(2, 3) == 5


def test_ensure_sync_wraps_coroutine_function():
    app = Veloce()

    async def add(a, b):
        return a + b

    wrapped = app.ensure_sync(add)
    # New sync callable invoked from non-async code.
    assert wrapped is not add
    assert wrapped(2, 3) == 5


def test_ensure_sync_preserves_function_name():
    app = Veloce()

    async def my_handler():
        return 1

    wrapped = app.ensure_sync(my_handler)
    assert wrapped.__name__ == "my_handler"


def test_ensure_sync_kwargs_passthrough():
    app = Veloce()

    async def greet(name, greeting="hi"):
        return f"{greeting}, {name}"

    wrapped = app.ensure_sync(greet)
    assert wrapped("alice", greeting="hello") == "hello, alice"


def test_ensure_sync_propagates_exception():
    app = Veloce()

    async def boom():
        raise RuntimeError("nope")

    wrapped = app.ensure_sync(boom)
    with pytest.raises(RuntimeError, match="nope"):
        wrapped()


def test_ensure_sync_callable_object_returned_unchanged():
    """A non-coroutine callable instance (e.g. a class with __call__)
    is treated as sync and returned as-is."""
    app = Veloce()

    class Caller:
        def __call__(self, x):
            return x + 1

    obj = Caller()
    wrapped = app.ensure_sync(obj)
    assert wrapped is obj
    assert wrapped(4) == 5
