"""Veloce.handle_http_exception / handle_user_exception / log_exception."""

from __future__ import annotations

import logging

from veloce import HTTPException, JSONResponse, Veloce
from veloce.exceptions import Forbidden, NotFound


async def test_handle_http_exception_default_body():
    app = Veloce(debug=True, openapi_url=None)
    resp = await app.handle_http_exception(NotFound("missing"))
    assert resp.status_code == 404
    import orjson

    # Same body the request cycle emits for the same exception - the two must
    # not diverge, or a handler reports errors differently over MCP than HTTP.
    assert orjson.loads(resp.body) == {"detail": "missing", "status_code": 404}


async def test_handle_http_exception_uses_status_handler():
    app = Veloce(debug=True, openapi_url=None)

    @app.errorhandler(404)
    async def custom(request, exc):
        return JSONResponse({"custom": True}, status_code=404)

    resp = await app.handle_http_exception(NotFound())
    import orjson

    assert orjson.loads(resp.body) == {"custom": True}


async def test_handle_http_exception_passes_headers():
    app = Veloce(debug=True, openapi_url=None)
    exc = Forbidden("nope")
    exc.headers = {"X-Reason": "private"}
    resp = await app.handle_http_exception(exc)
    assert resp.headers.get("X-Reason") == "private"


async def test_handle_user_exception_http_routes_through_http_path():
    app = Veloce(debug=True, openapi_url=None)
    resp = await app.handle_user_exception(NotFound("nope"))
    assert resp.status_code == 404


async def test_handle_user_exception_arbitrary_with_handler():
    app = Veloce(debug=True, openapi_url=None)

    class MyError(Exception):
        pass

    @app.errorhandler(MyError)
    async def handler(request, exc):
        return {"err": str(exc)}

    resp = await app.handle_user_exception(MyError("boom"))
    import orjson

    assert orjson.loads(resp.body) == {"err": "boom"}


async def test_handle_user_exception_unhandled_returns_500(caplog):
    app = Veloce(debug=True, openapi_url=None)
    caplog.set_level(logging.ERROR, logger=app.logger.name)

    class Random(Exception):
        pass

    resp = await app.handle_user_exception(Random("kaboom"))
    assert resp.status_code == 500
    # `log_exception` ran.
    assert any("Exception on request" in r.message for r in caplog.records)


def test_log_exception_calls_logger(caplog):
    app = Veloce(openapi_url=None)
    caplog.set_level(logging.ERROR, logger=app.logger.name)
    try:
        raise RuntimeError("test")
    except RuntimeError as e:
        app.log_exception(e)
    assert any("Exception on request" in r.message for r in caplog.records)


async def test_handle_http_exception_bare():
    """Untyped HTTPException (no detail/headers) gets sensible defaults."""
    app = Veloce(debug=True, openapi_url=None)
    resp = await app.handle_http_exception(HTTPException(418, "i am a teapot"))
    assert resp.status_code == 418
    import orjson

    assert orjson.loads(resp.body) == {"detail": "i am a teapot", "status_code": 418}


async def test_error_body_is_identical_across_http_and_mcp_doors():
    """The same handler raising the same exception must report it the same way
    on both doors. The HTTP path shapes errors in `_dispatch_request`, while the
    MCP path routes through `handle_user_exception`; those two builders drifting
    apart is invisible in tests that only exercise one door."""
    import orjson

    from veloce.contrib.mcp.server import MCPServer
    from veloce.contrib.mcp.transports.stdio import StdioTransport

    app = Veloce(openapi_url=None)

    @app.get("/items/{item_id}", expose_as_mcp_tool=True, mcp_description="Fetch an item")
    async def get_item(item_id: int):
        raise NotFound("item 7 does not exist")

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app._asgi_app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/items/7",
            "raw_path": b"/items/7",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"t")],
            "client": ("127.0.0.1", 5000),
            "server": ("127.0.0.1", 8000),
        },
        receive,
        send,
    )
    http_body = orjson.loads(b"".join(m.get("body", b"") for m in sent))

    inbox: list[bytes] = []
    outbox: list[dict] = []

    async def read_line():
        return inbox.pop(0) if inbox else None

    async def write_line(data: bytes):
        outbox.append(orjson.loads(data))

    transport = StdioTransport(MCPServer(app), read_line, write_line)
    for message in (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_item", "arguments": {"item_id": 7}},
        },
    ):
        inbox.append(orjson.dumps(message))
    await transport.serve()

    mcp_body = orjson.loads(outbox[-1]["result"]["content"][0]["text"])
    assert http_body == mcp_body
    assert http_body["status_code"] == 404
