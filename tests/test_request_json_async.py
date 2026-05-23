"""`Request.json()` async signature — pins #74."""

from __future__ import annotations

import inspect

from veloce import Request


def _req(body: bytes, content_type: str = "application/json") -> Request:
    return Request(
        method="POST",
        path="/x",
        query_string="",
        headers={"content-type": content_type},
        body=body,
    )


def test_request_json_is_coroutine_function():
    """`Request.json` must remain `async def`. A future contributor reverting
    it to a sync method would break the Starlette / FastAPI / Quart
    `await request.json()` idiom; this is the regression guard."""
    assert inspect.iscoroutinefunction(Request.json)


async def test_request_json_awaits_to_parsed_value():
    """End-to-end of the async API: `await request.json()` resolves to
    the parsed dict."""
    assert await _req(b'{"a": 1}').json() == {"a": 1}


async def test_request_json_empty_body_resolves_to_none():
    assert await _req(b"").json() is None


def test_request_get_json_stays_synchronous():
    """The Flask-shape `get_json()` keeps the sync signature so callers
    porting from Flask do not need to rewrite their code."""
    assert not inspect.iscoroutinefunction(Request.get_json)
