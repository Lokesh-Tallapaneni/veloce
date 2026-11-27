"""A `response_class` that can render a handler's value is allowed to.

Dispatch refuses a non-JSON `response_class` handed a `dict` or `list`, so the
obscure `AttributeError: 'dict' object has no attribute 'encode'` became a
`TypeError` naming the class and the remedy. That is right for a class that
cannot render structured data.

It was applied before trying, though, so it also refused a class that *can* -
any user-defined serialiser whose `__init__` renders the value. Those worked
before and answered 500 after, with the framework's own message insisting the
route declare `response_class=JSONResponse` instead.

The refusal now follows a real failure rather than predicting one.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import JSONResponse, Response, Veloce


class Yamlish(Response):
    """A user-defined serialiser: it renders whatever it is given."""

    def __init__(self, content=None, **kwargs) -> None:
        body = b"" if content is None else str(content).encode()
        super().__init__(body=body, content_type="text/x-yamlish", **kwargs)


class TextOnly(Response):
    """A class that genuinely cannot render structured data."""

    def __init__(self, content=None, **kwargs) -> None:
        super().__init__(body=content.encode(), content_type="text/plain", **kwargs)


async def _answer(response_class: type, value):
    app = Veloce(openapi_url=None)

    @app.get("/x", response_class=response_class)
    async def x():
        return value

    return await app.handle_request(make_request(path="/x"))


async def test_a_custom_class_that_renders_a_dict_still_does():
    """The regression: this answered 200 before and 500 after."""
    resp = await _answer(Yamlish, {"a": 1})

    assert resp.status_code == 200
    assert resp.content_type == "text/x-yamlish"
    assert resp.body == b"{'a': 1}"


async def test_a_custom_class_that_renders_a_list_still_does():
    resp = await _answer(Yamlish, [1, 2])

    assert resp.status_code == 200
    assert resp.body == b"[1, 2]"


async def test_a_custom_class_given_a_string_is_unaffected():
    resp = await _answer(Yamlish, "plain")

    assert resp.status_code == 200
    assert resp.body == b"plain"


def test_a_class_that_cannot_render_a_dict_still_says_so_clearly():
    """The improvement this refusal was written for has to survive the fix.

    Against `_coerce_response` rather than through a request: dispatch turns the
    `TypeError` into a 500, so the message a developer is meant to read is only
    visible at the point it is raised.
    """
    app = Veloce(openapi_url=None)

    with pytest.raises(TypeError, match="TextOnly"):
        app._coerce_response({"a": 1}, TextOnly)


def test_the_message_still_names_the_remedy():
    """A reader has to be told what to do, not only what went wrong."""
    app = Veloce(openapi_url=None)

    with pytest.raises(TypeError, match="response_class=JSONResponse"):
        app._coerce_response({"a": 1}, TextOnly)


def test_the_refusal_keeps_the_cause_it_replaced():
    """The `AttributeError` this message stands in for should still be reachable.

    Chained rather than discarded, so a report of the clear message still
    carries what actually failed underneath it.
    """
    app = Veloce(openapi_url=None)

    with pytest.raises(TypeError) as raised:
        app._coerce_response({"a": 1}, TextOnly)

    assert raised.value.__cause__ is not None, "the underlying failure was discarded"


async def test_a_json_response_class_is_unaffected():
    resp = await _answer(JSONResponse, {"a": 1})

    assert resp.status_code == 200
    assert b'"a"' in resp.body
