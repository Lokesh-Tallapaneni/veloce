"""A tuple return means the same thing with and without `response_class`.

`_coerce_response` unpacked the `(body, status[, headers])` return twice, once in
each branch, and the two copies had drifted. Only the 2- and 3-tuple are
documented; the copies disagreed about everything else:

    @app.get("/a", response_class=JSONResponse)
    async def a(): return ("x",)          # -> "x"

    @app.get("/b")
    async def b(): return ("x",)          # -> ["x"]

and a 4-tuple was worse than inconsistent - the `response_class` copy read
`result[0]` and **silently discarded** the rest, so a mistyped
`return body, 201, headers, extra` served `200` with no headers and said nothing.

Both now go through one unpacking. A tuple that is not a response tuple is a
value, which is what the no-class branch already did and what the reference
table documents.

The tests below assert the two doors *against each other* rather than against a
fixed expectation, so a future change that alters one and not the other fails
here even if the new behaviour is defensible on its own.
"""

from __future__ import annotations

import pytest

from veloce import JSONResponse, PlainTextResponse, Veloce, status
from veloce.testclient import TestClient


def _serve(value, response_class=None):
    app = Veloce(openapi_url=None)
    kwargs = {"response_class": response_class} if response_class is not None else {}

    @app.get("/r", **kwargs)
    async def route():
        return value

    return TestClient(app).get("/r")


def _both(value):
    """The same return through both branches."""
    return _serve(value), _serve(value, JSONResponse)


# ── the documented shapes, unchanged ─────────────────────────────────


@pytest.mark.parametrize("response_class", [None, JSONResponse])
def test_a_two_tuple_applies_its_status(response_class):
    resp = _serve(({"ok": True}, 201), response_class)
    assert resp.status_code == 201
    assert resp.json() == {"ok": True}


@pytest.mark.parametrize("response_class", [None, JSONResponse])
def test_a_three_tuple_applies_status_and_headers(response_class):
    resp = _serve(({"ok": True}, 201, {"X-Trace": "abc"}), response_class)
    assert resp.status_code == 201
    assert resp.headers["X-Trace"] == "abc"
    assert resp.json() == {"ok": True}


@pytest.mark.parametrize("response_class", [None, JSONResponse])
def test_a_two_tuple_with_headers_keeps_status_200(response_class):
    """The second element may be a headers dict instead of a status."""
    resp = _serve(({"ok": True}, {"X-Trace": "abc"}), response_class)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.headers["X-Trace"] == "abc"


@pytest.mark.parametrize("response_class", [None, JSONResponse])
def test_a_string_status_is_coerced(response_class):
    resp = _serve(({"ok": True}, "201"), response_class)
    assert resp.status_code == 201


def test_a_named_status_constant_works():
    assert _serve(({"ok": True}, status.HTTP_202_ACCEPTED)).status_code == 202


def test_a_str_body_in_a_tuple_is_still_html():
    """The documented caveat: `("<b>hi</b>", 201)` is an HTML 201."""
    resp = _serve(("<b>hi</b>", 201))
    assert resp.status_code == 201
    assert "text/html" in resp.headers["content-type"]


# ── the shapes that disagreed ────────────────────────────────────────


def test_a_one_tuple_means_the_same_thing_on_both_doors():
    """The defect: `"x"` on one door and `["x"]` on the other."""
    plain, classed = _both(("x",))
    assert plain.text == classed.text


def test_a_one_tuple_is_a_value():
    """It is not a response tuple, so it is the value - as the table says."""
    assert _serve(("x",)).json() == ["x"]
    assert _serve(("x",), JSONResponse).json() == ["x"]


def test_a_four_tuple_means_the_same_thing_on_both_doors():
    plain, classed = _both(("x", 201, {"X-H": "1"}, "extra"))
    assert plain.text == classed.text
    assert plain.status_code == classed.status_code


def test_a_four_tuple_does_not_silently_drop_its_tail():
    """The worse half: `result[0]` was taken and the rest discarded in silence.

    A mistyped `return body, 201, headers, extra` served a 200 with no headers.
    Emitting the whole tuple is at least visible in the response.
    """
    resp = _serve(("x", 201, {"X-H": "1"}, "extra"), JSONResponse)
    assert resp.json() == ["x", 201, {"X-H": "1"}, "extra"]


def test_an_empty_tuple_means_the_same_thing_on_both_doors():
    plain, classed = _both(())
    assert plain.text == classed.text
    assert plain.json() == []


def test_a_five_tuple_means_the_same_thing_on_both_doors():
    plain, classed = _both((1, 2, 3, 4, 5))
    assert plain.json() == classed.json() == [1, 2, 3, 4, 5]


# ── the response_class is still honoured ─────────────────────────────


def test_the_requested_class_renders_the_tuple_body():
    resp = _serve(("plain text", 201), PlainTextResponse)
    assert resp.status_code == 201
    assert resp.text == "plain text"
    assert "text/plain" in resp.headers["content-type"]


def test_the_requested_class_survives_a_three_tuple():
    resp = _serve(("plain text", 201, {"X-H": "1"}), PlainTextResponse)
    assert resp.headers["X-H"] == "1"
    assert "text/plain" in resp.headers["content-type"]


def test_a_class_that_cannot_render_the_value_still_raises():
    """A `PlainTextResponse` route returning a dict is a mismatch, not a coercion.

    The negative: consolidating the unpacking must not turn a declared type
    error into a silent JSON body.
    """
    assert _serve({"a": 1}, PlainTextResponse).status_code == 500


def test_a_non_response_tuple_under_a_text_class_is_still_a_mismatch():
    assert _serve((1, 2, 3, 4), PlainTextResponse).status_code == 500


# ── the status/header mutation is not left stale ─────────────────────
#
# The no-class copy cleared the response's cached encoding after writing the
# status; the `response_class` copy did not. Sharing one unpacking makes that a
# single decision rather than two.


@pytest.mark.parametrize("response_class", [None, JSONResponse])
def test_the_status_on_the_wire_is_the_tuple_status(response_class):
    resp = _serve(({"ok": True}, 418), response_class)
    assert resp.status_code == 418


@pytest.mark.parametrize("response_class", [None, JSONResponse])
def test_a_header_added_by_the_tuple_reaches_the_wire(response_class):
    resp = _serve(({"ok": True}, 201, {"X-Late": "yes"}), response_class)
    assert resp.headers["X-Late"] == "yes"


@pytest.mark.parametrize("response_class", [None, JSONResponse])
def test_a_nested_response_body_keeps_the_outer_status(response_class):
    """`(Response(...), 201)` - the body is already a Response."""
    resp = _serve((JSONResponse({"ok": True}), 201), response_class)
    assert resp.status_code == 201
    assert resp.json() == {"ok": True}


# ── other body types inside a tuple, on both doors ───────────────────


@pytest.mark.parametrize("response_class", [None, JSONResponse])
def test_a_list_body_in_a_tuple(response_class):
    resp = _serve(([1, 2], 201), response_class)
    assert resp.status_code == 201
    assert resp.json() == [1, 2]


@pytest.mark.parametrize("response_class", [None, JSONResponse])
def test_a_pydantic_body_in_a_tuple(response_class):
    from pydantic import BaseModel

    class Item(BaseModel):
        id: int

    resp = _serve((Item(id=1), 201), response_class)
    assert resp.status_code == 201
    assert resp.json() == {"id": 1}


def test_a_msgspec_body_in_a_tuple():
    msgspec = pytest.importorskip("msgspec")

    class Item(msgspec.Struct):
        id: int

    resp = _serve((Item(id=1), 201))
    assert resp.status_code == 201
    assert resp.json() == {"id": 1}
