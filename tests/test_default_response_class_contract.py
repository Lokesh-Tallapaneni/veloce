"""A response class that cannot render a value says so, by name.

`default_response_class=HTMLResponse` with a handler returning a dict produced:

    AttributeError: 'dict' object has no attribute 'encode'
    500 {"detail": "Internal Server Error"}

A text response class encodes `str` or `bytes`; it was handed a mapping and the
failure surfaced from inside `Response.render`, naming neither the class that was
asked for nor what to do instead.

The documentation prose claimed the opposite — that "a dict returned from this
handler is rendered as HTML-bytes by `HTMLResponse` rather than JSON" — while its
own example returned a string. The prose was wrong, and is corrected alongside
this; there is no sensible rendering of a mapping as HTML, so the honest outcome
is a clear error rather than a silent fallback to JSON that would contradict the
class the route declared.

Error responses deliberately still answer JSON. `default_response_class` is the
fallback for what a *handler returns*; an error body is a separate contract with
a documented shape (`{"detail": ..., "status_code": ...}`) that clients parse.
That is tested here so the boundary is explicit rather than incidental.
"""

from __future__ import annotations

import logging

import pytest

from veloce import (
    HTMLResponse,
    HTTPException,
    JSONResponse,
    ORJSONResponse,
    PlainTextResponse,
    Response,
    Veloce,
)
from veloce.testclient import TestClient

TEXT_CLASSES = [HTMLResponse, PlainTextResponse]
JSON_CLASSES = [JSONResponse, ORJSONResponse]


@pytest.fixture(autouse=True)
def _quiet_500s():
    """A deliberate 500 logs a traceback; keep the run readable."""
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def _app(**kwargs) -> Veloce:
    return Veloce(openapi_url=None, **kwargs)


# ── the mismatch is reported ─────────────────────────────────────────


@pytest.mark.parametrize("cls", TEXT_CLASSES)
@pytest.mark.parametrize("value", [{"a": 1}, [1, 2], {"nested": {"b": 2}}, []])
def test_a_text_class_given_a_mapping_or_list_raises(cls, value):
    """The defect: an AttributeError from inside the renderer."""
    app = _app(default_response_class=cls, debug=True)

    @app.get("/x")
    async def x():
        return value

    body = TestClient(app).get("/x").text
    assert "TypeError" in body
    assert f"{cls.__name__} cannot render" in body


@pytest.mark.parametrize("cls", TEXT_CLASSES)
def test_the_message_names_the_returned_type(cls):
    app = _app(default_response_class=cls, debug=True)

    @app.get("/x")
    async def x():
        return {"a": 1}

    assert "cannot render a dict" in TestClient(app).get("/x").text


def test_the_message_says_what_to_do_instead():
    app = _app(default_response_class=HTMLResponse, debug=True)

    @app.get("/x")
    async def x():
        return {"a": 1}

    body = TestClient(app).get("/x").text
    assert "response_class=JSONResponse" in body


def test_a_route_level_class_reports_the_same_way():
    app = _app(debug=True)

    @app.get("/x", response_class=HTMLResponse)
    async def x():
        return {"a": 1}

    assert "HTMLResponse cannot render a dict" in TestClient(app).get("/x").text


def test_it_is_a_500_not_a_silent_wrong_body():
    """A mapping rendered as its Python repr would be worse than an error."""
    app = _app(default_response_class=HTMLResponse)

    @app.get("/x")
    async def x():
        return {"a": 1}

    response = TestClient(app).get("/x")
    assert response.status_code == 500
    assert "{'a': 1}" not in response.text


# ── what a text class does render ────────────────────────────────────


@pytest.mark.parametrize("cls", TEXT_CLASSES)
def test_a_string_is_rendered(cls):
    app = _app(default_response_class=cls)

    @app.get("/x")
    async def x():
        return "hello"

    response = TestClient(app).get("/x")
    assert response.status_code == 200
    assert response.text == "hello"


@pytest.mark.parametrize("cls", TEXT_CLASSES)
def test_bytes_are_rendered(cls):
    app = _app(default_response_class=cls)

    @app.get("/x")
    async def x():
        return b"hello"

    assert TestClient(app).get("/x").text == "hello"


def test_the_media_type_is_the_class_default():
    app = _app(default_response_class=HTMLResponse)

    @app.get("/x")
    async def x():
        return "<b>hi</b>"

    assert TestClient(app).get("/x").headers["content-type"].startswith("text/html")


def test_a_plain_text_class_uses_its_own_media_type():
    app = _app(default_response_class=PlainTextResponse)

    @app.get("/x")
    async def x():
        return "hi"

    assert TestClient(app).get("/x").headers["content-type"].startswith("text/plain")


def test_a_returned_response_instance_still_wins():
    """Documented: the instance is sent as-is, whatever the class."""
    app = _app(default_response_class=HTMLResponse)

    @app.get("/x")
    async def x():
        return JSONResponse({"a": 1})

    response = TestClient(app).get("/x")
    assert response.json() == {"a": 1}
    assert response.headers["content-type"].startswith("application/json")


def test_a_route_class_overrides_the_app_default():
    app = _app(default_response_class=HTMLResponse)

    @app.get("/x", response_class=JSONResponse)
    async def x():
        return {"a": 1}

    assert TestClient(app).get("/x").json() == {"a": 1}


# ── a JSON class still takes everything ──────────────────────────────


@pytest.mark.parametrize("cls", JSON_CLASSES)
@pytest.mark.parametrize("value", [{"a": 1}, [1, 2], {"nested": {"b": 2}}])
def test_a_json_class_renders_a_mapping_or_list(cls, value):
    app = _app(default_response_class=cls)

    @app.get("/x")
    async def x():
        return value

    response = TestClient(app).get("/x")
    assert response.status_code == 200
    assert response.json() == value


def test_the_documented_orjson_example_works():
    """From `responses-advanced.md`, verbatim."""
    app = Veloce(openapi_url=None, default_response_class=ORJSONResponse)

    @app.get("/items")
    async def items():
        return [{"id": 1}, {"id": 2}]

    assert TestClient(app).get("/items").json() == [{"id": 1}, {"id": 2}]


def test_the_default_is_unchanged_with_no_class_set():
    app = _app()

    @app.get("/x")
    async def x():
        return {"a": 1}

    response = TestClient(app).get("/x")
    assert response.json() == {"a": 1}
    assert response.headers["content-type"].startswith("application/json")


def test_a_string_with_no_class_set_is_still_html():
    app = _app()

    @app.get("/x")
    async def x():
        return "<b>hi</b>"

    assert TestClient(app).get("/x").headers["content-type"].startswith("text/html")


# ── error bodies keep their own contract ─────────────────────────────


@pytest.mark.parametrize("cls", TEXT_CLASSES)
def test_a_404_is_still_json(cls):
    """`default_response_class` is the fallback for a handler's return value."""
    app = _app(default_response_class=cls)

    @app.get("/x")
    async def x():
        return "ok"

    response = TestClient(app).get("/missing")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()


@pytest.mark.parametrize("cls", TEXT_CLASSES)
def test_a_405_is_still_json(cls):
    app = _app(default_response_class=cls)

    @app.get("/x")
    async def x():
        return "ok"

    assert TestClient(app).post("/x").headers["content-type"].startswith("application/json")


def test_a_raised_http_exception_is_still_json():

    app = _app(default_response_class=HTMLResponse)

    @app.get("/x")
    async def x():
        raise HTTPException(status_code=418, detail="teapot")

    response = TestClient(app).get("/x")
    assert response.status_code == 418
    assert response.json()["detail"] == "teapot"


def test_an_explicit_error_response_is_still_honoured():
    """A handler that wants a text error can still return one."""
    app = _app(default_response_class=HTMLResponse)

    @app.get("/x")
    async def x():
        return Response(body=b"gone", status_code=410, content_type="text/plain")

    response = TestClient(app).get("/x")
    assert response.status_code == 410
    assert response.text == "gone"


# ── the tuple form goes through the same class ───────────────────────


def test_a_tuple_of_string_and_status_is_rendered():
    app = _app(default_response_class=HTMLResponse)

    @app.get("/x")
    async def x():
        return "<b>made</b>", 201

    response = TestClient(app).get("/x")
    assert response.status_code == 201
    assert response.text == "<b>made</b>"


def test_a_tuple_carrying_a_dict_body_reports_the_mismatch():
    app = _app(default_response_class=HTMLResponse, debug=True)

    @app.get("/x")
    async def x():
        return {"a": 1}, 201

    assert "cannot render a dict" in TestClient(app).get("/x").text
