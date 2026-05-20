"""Cookies MultiDict + RequestValidationError tests (Q13, E5)."""

from __future__ import annotations

import pytest

from veloce import (
    HTTPException,
    Query,
    Request,
    RequestValidationError,
    ValidationError,
    Veloce,
)
from veloce.http.datastructures import Cookies

# ── Q13: Cookies MultiDict ─────────────────────────────────────────────


def test_cookies_parses_single_cookie():
    c = Cookies.from_cookie_header("session=abc123")
    assert c["session"] == "abc123"
    assert c.getlist("session") == ["abc123"]


def test_cookies_parses_multiple_distinct_cookies():
    c = Cookies.from_cookie_header("a=1; b=2; c=3")
    assert c["a"] == "1"
    assert c["b"] == "2"
    assert c["c"] == "3"


def test_cookies_preserves_duplicate_names():
    """Same name twice — both values retained."""
    c = Cookies.from_cookie_header("tag=x; tag=y; other=z")
    assert c.getlist("tag") == ["x", "y"]
    assert c["tag"] == "x"  # first wins for single-value access
    assert c["other"] == "z"


def test_cookies_strips_whitespace():
    c = Cookies.from_cookie_header(" a=1 ;  b=2 ")
    assert c["a"] == "1"
    assert c["b"] == "2"


def test_cookies_skips_attributes_without_value():
    """Attributes like `Secure` or `HttpOnly` belong on Set-Cookie, not
    Cookie — but if they appear, skip them silently."""
    c = Cookies.from_cookie_header("a=1; Secure; b=2")
    assert c["a"] == "1"
    assert c["b"] == "2"
    assert c.getlist("Secure") == []


def test_cookies_getlist_missing_returns_empty():
    c = Cookies.from_cookie_header("a=1")
    assert c.getlist("missing") == []


def test_cookies_empty_header():
    c = Cookies.from_cookie_header("")
    assert len(c) == 0


def test_request_cookies_is_cookies_instance():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={"cookie": "x=1; y=2"},
        body=b"",
    )
    assert isinstance(req.cookies, Cookies)
    assert req.cookies["x"] == "1"


# ── E5: RequestValidationError distinct from ValidationError ──────────


def test_request_validation_error_is_subclass_of_validation_error():
    """For back-compat: existing `except ValidationError` continues to
    catch application-level subclass."""
    assert issubclass(RequestValidationError, ValidationError)
    assert issubclass(RequestValidationError, HTTPException)
    assert RequestValidationError([{"loc": ["x"], "msg": "m", "type": "t"}]).status_code == 422


@pytest.mark.asyncio
async def test_resolver_raises_request_validation_error_specifically():
    """A missing required Query parameter must raise the new subclass —
    not the bare ValidationError."""
    app = Veloce(debug=True, openapi_url=None)

    captured: dict = {}

    @app.exception_handler(RequestValidationError)
    async def on_req_validation(request, exc):
        from veloce import JSONResponse

        captured["exc_type"] = type(exc).__name__
        return JSONResponse({"req_validation": True}, status_code=422)

    @app.get("/items")
    async def items(q: str = Query()):  # no default => required
        return {"q": q}

    req = Request(method="GET", path="/items", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert resp.status_code == 422
    assert captured["exc_type"] == "RequestValidationError"
    assert b'"req_validation":true' in resp.body


@pytest.mark.asyncio
async def test_existing_validation_error_handler_still_catches():
    """A handler registered for ValidationError must still catch the new
    subclass via the MRO walk introduced in E3."""
    app = Veloce(debug=True, openapi_url=None)

    @app.exception_handler(ValidationError)
    async def on_val(request, exc):
        from veloce import JSONResponse

        return JSONResponse(
            {"caught_via": "ValidationError", "subclass": type(exc).__name__},
            status_code=422,
        )

    @app.get("/x")
    async def x(q: str = Query()):  # required → triggers framework validation
        return {"q": q}

    import orjson

    req = Request(method="GET", path="/x", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    body = orjson.loads(resp.body)
    assert body["caught_via"] == "ValidationError"
    # The actual instance is a RequestValidationError under the hood.
    assert body["subclass"] == "RequestValidationError"


def test_request_validation_error_in_exports():
    """`from veloce import RequestValidationError` works."""
    from veloce import RequestValidationError as RVE_top  # noqa: N814

    assert RVE_top is RequestValidationError
