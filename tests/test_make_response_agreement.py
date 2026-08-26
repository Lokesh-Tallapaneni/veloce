"""The three handler-return coercion tables answer alike.

`Veloce.make_response`'s docstring asserts that all three implementations of the
`(body, status[, headers])` table - `veloce.make_response` (helpers.py),
`Veloce.make_response` (app/core.py) and `DispatchMixin._coerce_response` - apply
the same rules and "answer alike". They did not:

    make_response(Response(body=b"resp", status_code=201))

    veloce.make_response -> JSONResponse 200, body b'"<veloce.http.response.Response object at 0x...>"'
    Veloce.make_response -> the Response, unchanged
    dispatch             -> the Response, unchanged

The standalone helper had no `Response` pass-through, so a `Response` fell to
its final JSON branch and was encoded through `__str__` - the body became the
object's repr. And raw `bytes` came back as `application/octet-stream` from the
helper but `text/html` from the other two, so the same value carried a different
content type depending on which entry point a caller reached for.

Both were unified onto what the other two already did (which is also what Flask's
`text/html` default mimetype does for a bytes body). These tests assert the three
against **each other** across the whole table, so the docstring's claim is
checked rather than asserted.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from veloce import Response, Veloce
from veloce.helpers import make_response as helper_make_response
from veloce.testclient import TestClient


class Model(BaseModel):
    a: int


def _app() -> Veloce:
    return Veloce(openapi_url=None)


def _shape(response: Response) -> tuple:
    """The observable result of a coercion, for comparing implementations."""
    return (type(response).__name__, response.status_code, response.body, response.content_type)


VALUES = {
    "str": lambda: "hello",
    "bytes": lambda: b"raw",
    "dict": lambda: {"a": 1},
    "list": lambda: [1, 2],
    "int": lambda: 42,
    "none": lambda: None,
    "model": lambda: Model(a=1),
    "response": lambda: Response(body=b"resp", status_code=201, content_type="text/plain"),
    "tuple-status": lambda: ("hi", 201),
    "tuple-bytes-status": lambda: (b"hi", 201),
    "tuple-headers": lambda: ("hi", {"X-A": "1"}),
    "tuple-status-headers": lambda: ("hi", 201, {"X-A": "1"}),
    "tuple-dict-status": lambda: ({"a": 1}, 201),
}


# ── the two make_response entry points agree ─────────────────────────


@pytest.mark.parametrize("name", list(VALUES))
def test_the_two_make_response_entry_points_agree(name):
    """The claim in `Veloce.make_response`'s own docstring, checked."""
    app = _app()
    assert _shape(helper_make_response(VALUES[name]())) == _shape(app.make_response(VALUES[name]()))


def test_a_response_passes_through_the_helper_untouched():
    """The defect: it was JSON-encoded through `__str__` into its own repr."""
    original = Response(body=b"resp", status_code=201, content_type="text/plain")
    result = helper_make_response(original)
    assert result is original
    assert result.body == b"resp"
    assert result.status_code == 201


def test_the_helper_does_not_stringify_a_response():
    """Stated as the symptom, so a regression is recognisable."""
    body = helper_make_response(Response(body=b"resp")).body
    assert b"veloce.http.response" not in body
    assert b"object at 0x" not in body


def test_bytes_carry_the_same_content_type_everywhere():
    """The helper answered `application/octet-stream` where the others said
    `text/html`."""
    app = _app()
    assert helper_make_response(b"raw").content_type == app.make_response(b"raw").content_type


def test_an_explicit_content_type_still_wins():
    """The negative: unifying the default must not ignore the argument."""
    resp = helper_make_response(b"raw", content_type="application/pdf")
    assert resp.content_type == "application/pdf"


# ── and dispatch agrees with both ────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["str", "bytes", "dict", "list", "int", "none", "model", "response"],
)
def test_dispatch_agrees_with_make_response(name):
    """The third implementation - the one that actually runs for a handler."""
    factory = VALUES[name]
    app = _app()

    @app.get("/x")
    async def x():
        return factory()

    wire = TestClient(app).get("/x")
    coerced = app.make_response(factory())
    assert wire.status_code == coerced.status_code
    assert wire.body == coerced.body
    assert wire.headers.get("content-type") == coerced.content_type


def test_a_handler_returning_a_response_with_a_status_tuple():
    """`return resp, 404` applies the status in dispatch; the helper's tuple
    path must do the same."""
    app = _app()

    @app.get("/x")
    async def x():
        return Response(body=b"r", status_code=201), 404

    assert TestClient(app).get("/x").status_code == 404
    assert helper_make_response((Response(body=b"r", status_code=201), 404)).status_code == 404


# ── the tuple table itself ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "status", "header"),
    [
        (("hi", 201), 201, None),
        (("hi", {"X-A": "1"}), 200, "1"),
        (("hi", 201, {"X-A": "1"}), 201, "1"),
        ((b"hi", 201), 201, None),
        (({"a": 1}, 201), 201, None),
    ],
)
def test_the_tuple_shapes_unpack_the_same_way(value, status, header):
    app = _app()
    for coerce in (helper_make_response, app.make_response):
        resp = coerce(value)
        assert resp.status_code == status
        if header is not None:
            assert resp.headers["X-A"] == header


def test_a_single_element_tuple_is_just_the_body():
    app = _app()
    assert _shape(helper_make_response(("hi",))) == _shape(app.make_response(("hi",)))
