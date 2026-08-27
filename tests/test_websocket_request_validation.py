"""WebSocketRequestValidationError — WS dependency validation failure (V9)."""

from __future__ import annotations

import asyncio

from veloce import (
    Depends,
    RequestValidationError,
    Veloce,
    WebSocket,
    WebSocketRequestValidationError,
)
from veloce import WebSocketRequestValidationError as Exc


def _run_ws(app: Veloce, path: str, query_string: bytes = b"") -> list[dict]:
    """Drive one WebSocket connection through the ASGI surface."""
    scope = {
        "type": "websocket",
        "path": path,
        "headers": [],
        "query_string": query_string,
    }
    incoming = [{"type": "websocket.connect"}]
    sent: list[dict] = []

    async def receive() -> dict:
        if incoming:
            return incoming.pop(0)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict) -> None:
        sent.append(message)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app(scope, receive, send))
    finally:
        loop.close()
    return sent


def _require_token(ws: WebSocket):
    token = ws.query_params.get("token")
    if not token:
        raise RequestValidationError(
            [{"loc": ["query", "token"], "msg": "field required", "type": "missing"}]
        )
    return token


def test_error_is_subclass_of_request_validation_error():
    assert issubclass(WebSocketRequestValidationError, RequestValidationError)


def test_error_carries_errors_list():
    exc = WebSocketRequestValidationError([{"loc": ["query", "x"], "msg": "bad"}])
    assert exc.errors == [{"loc": ["query", "x"], "msg": "bad"}]


def test_missing_dependency_param_closes_with_1008():
    app = Veloce()

    @app.websocket("/ws")
    async def handler(ws: WebSocket, token=Depends(_require_token)):
        await ws.accept()

    sent = _run_ws(app, "/ws")  # no ?token
    close = [m for m in sent if m["type"] == "websocket.close"][0]
    assert close["code"] == 1008


def test_valid_dependency_lets_handler_run():
    app = Veloce()
    seen: dict = {}

    @app.websocket("/ws")
    async def handler(ws: WebSocket, token=Depends(_require_token)):
        await ws.accept()
        seen["token"] = token

    sent = _run_ws(app, "/ws", query_string=b"token=abc")
    assert seen.get("token") == "abc"
    # Handler ran and the connection was accepted.
    assert any(m["type"] == "websocket.accept" for m in sent)


def test_validation_failure_is_swallowed():
    app = Veloce()

    @app.websocket("/ws")
    async def handler(ws: WebSocket, token=Depends(_require_token)):
        await ws.accept()

    # No exception escapes — _run_ws completes cleanly.
    sent = _run_ws(app, "/ws")
    assert any(m["type"] == "websocket.close" for m in sent)


def test_importable_from_package_root():

    assert Exc is WebSocketRequestValidationError
