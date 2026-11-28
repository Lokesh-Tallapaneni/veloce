"""WebSocketRequestValidationError — WS dependency validation failure (V9)."""

from __future__ import annotations

from tests._ws_drive import run_ws
from veloce import (
    Depends,
    RequestValidationError,
    Veloce,
    WebSocket,
    WebSocketRequestValidationError,
)


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

    sent = run_ws(app, "/ws")  # no ?token
    close = [m for m in sent if m["type"] == "websocket.close"][0]
    assert close["code"] == 1008


def test_valid_dependency_lets_handler_run():
    app = Veloce()
    seen: dict = {}

    @app.websocket("/ws")
    async def handler(ws: WebSocket, token=Depends(_require_token)):
        await ws.accept()
        seen["token"] = token

    sent = run_ws(app, "/ws", query_string=b"token=abc")
    assert seen.get("token") == "abc"
    # Handler ran and the connection was accepted.
    assert any(m["type"] == "websocket.accept" for m in sent)


def test_validation_failure_is_swallowed():
    app = Veloce()

    @app.websocket("/ws")
    async def handler(ws: WebSocket, token=Depends(_require_token)):
        await ws.accept()

    # No exception escapes — _run_ws completes cleanly.
    sent = run_ws(app, "/ws")
    assert any(m["type"] == "websocket.close" for m in sent)


def test_request_validation_error_is_importable_from_package_root():
    """The import *is* the assertion, as in the sibling module.

    This compared `Exc` against `WebSocketRequestValidationError`, which the two
    module-top imports bind to the same object - so it held whatever the export
    did, and would have held with the name absent from `veloce.__all__`.
    """
    # Deferred deliberately: the import is what this test asserts.
    from veloce import WebSocketRequestValidationError as Imported

    assert issubclass(Imported, Exception)
    assert Imported.__module__.startswith("veloce.")
