"""veloce.exception_handlers default handlers."""

from __future__ import annotations

import orjson
import pytest

from veloce.exceptions import (
    HTTPException,
    NotFound,
    RequestValidationError,
    http_exception_handler,
    request_validation_exception_handler,
)


@pytest.mark.asyncio
async def test_http_exception_handler_renders_detail():
    resp = await http_exception_handler(None, HTTPException(404, "missing"))
    assert resp.status_code == 404
    assert orjson.loads(resp.body) == {"detail": "missing"}


@pytest.mark.asyncio
async def test_http_exception_handler_uses_subclass_description():
    resp = await http_exception_handler(None, NotFound())
    assert resp.status_code == 404
    # Falls back to the subclass description when no detail given.
    assert "detail" in orjson.loads(resp.body)


@pytest.mark.asyncio
async def test_http_exception_handler_propagates_headers():
    exc = HTTPException(401, "no", headers={"WWW-Authenticate": "Basic"})
    resp = await http_exception_handler(None, exc)
    assert resp.headers["WWW-Authenticate"] == "Basic"


@pytest.mark.asyncio
async def test_request_validation_handler_renders_422():
    errors = [{"loc": ["query", "x"], "msg": "required", "type": "missing"}]
    resp = await request_validation_exception_handler(None, RequestValidationError(errors))
    assert resp.status_code == 422
    assert orjson.loads(resp.body) == {"detail": errors}


@pytest.mark.asyncio
async def test_request_validation_handler_empty_errors():
    resp = await request_validation_exception_handler(None, RequestValidationError([]))
    assert orjson.loads(resp.body) == {"detail": []}
