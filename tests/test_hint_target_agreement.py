"""The document and the resolver resolve a handler's hints the same way.

`_inspect_handler` follows the indirection `inspect.signature` follows - a
`functools.partial` to its `func`, a class to `__init__`, a callable instance to
`type(obj).__call__` - before reading type hints, because `get_type_hints` does
not. The OpenAPI builder read hints off the handler object directly, so for any
handler that is not a plain function the document described a signature the
resolver was not using: a callable-instance handler published no parameters at
all while the resolver bound and validated two.

Both now resolve the target through one helper, so they cannot disagree.
"""

from __future__ import annotations

import functools

from pydantic import BaseModel

from veloce._handler_plan import _inspect_handler
from veloce._model_backend import resolve_return_model
from veloce._ws_listener import _message_annotation
from veloce.contrib.openapi import _handler_intro


class _Item(BaseModel):
    a: int


class _Msg(BaseModel):
    t: str


class _CallableHandler:
    async def __call__(self, limit: int = 1, tag: str = "x") -> _Item:
        return _Item(a=limit)


async def _returning(limit: int = 1) -> _Item:
    return _Item(a=limit)


async def _ws_listener_fn(socket, data: _Msg):
    return None


async def _untyped_listener(socket, data):
    return None


async def _plain(limit: int = 1, tag: str = "x"):
    return {"limit": limit, "tag": tag}


def test_a_callable_instance_resolves_the_same_hints_for_both():
    handler = _CallableHandler()
    _, plan_hints = _inspect_handler(handler)
    _, doc_hints = _handler_intro(handler)
    assert {"limit": int, "tag": str}.items() <= plan_hints.items()
    assert doc_hints == plan_hints


def test_a_partial_resolves_the_same_hints_for_both():
    handler = functools.partial(_plain, 1)
    _, plan_hints = _inspect_handler(handler)
    _, doc_hints = _handler_intro(handler)
    assert doc_hints == plan_hints


def test_a_plain_function_is_unchanged():
    """The negative: the common case must read exactly as before."""
    _, plan_hints = _inspect_handler(_plain)
    _, doc_hints = _handler_intro(_plain)
    assert {"limit": int, "tag": str}.items() <= plan_hints.items()
    assert doc_hints == plan_hints


def test_an_uninspectable_handler_still_degrades_rather_than_raising():
    """The document must keep degrading to no hints; it does not refuse a build."""
    _, hints = _handler_intro(len)
    assert hints == {}


def test_the_return_annotation_door_resolves_the_same_target():
    """Third door: a callable instance published its parameters while getting
    no `response_model` and no MCP `outputSchema`."""
    assert resolve_return_model(_CallableHandler()) is _Item
    assert resolve_return_model(functools.partial(_returning)) is _Item
    assert resolve_return_model(_returning) is _Item


def test_the_websocket_door_resolves_the_same_target():
    """Fourth door: a partial-wrapped listener accepted every frame
    unvalidated, where an unresolvable message annotation must be refused."""
    assert _message_annotation(_ws_listener_fn, True) is _Msg
    assert _message_annotation(functools.partial(_ws_listener_fn), True) is _Msg


def test_a_plain_callable_is_unaffected_on_both_doors(recwarn):
    """The negative: the common shapes keep their existing answers."""
    assert resolve_return_model(_plain) is None
    assert _message_annotation(_untyped_listener, True) is None
