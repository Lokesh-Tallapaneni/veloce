"""MCP error hierarchy - JSON-RPC code mapping, `to_error` rendering, dispatch."""

from __future__ import annotations

import orjson

from veloce import Veloce
from veloce.contrib.mcp import (
    AuthorizationError,
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    MCPError,
    MethodNotFoundError,
    OriginNotAllowedError,
    ProtocolVersionError,
    ResourceNotFoundError,
)
from veloce.contrib.mcp.errors import (
    _JSONRPC_FORBIDDEN,
    _JSONRPC_INTERNAL_ERROR,
    _JSONRPC_INVALID_PARAMS,
    _JSONRPC_INVALID_REQUEST,
    _JSONRPC_METHOD_NOT_FOUND,
    _JSONRPC_RESOURCE_NOT_FOUND,
    _ForbiddenError,
    _InBandError,
    _StreamTimeoutError,
    _StreamTooLargeError,
)
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.transports.stdio import StdioTransport


def _server(app: Veloce) -> MCPServer:
    return MCPServer(app)


class _Pipe:
    """Drive a `StdioTransport` in-process: feed request lines, collect replies."""

    def __init__(self, server: MCPServer) -> None:
        self._inbox: list[bytes] = []
        self.outbox: list[dict] = []
        self.transport = StdioTransport(server, self._read_line, self._write_line)

    def feed(self, message: dict) -> None:
        self._inbox.append(orjson.dumps(message))

    async def _read_line(self) -> bytes | None:
        if not self._inbox:
            return None
        return self._inbox.pop(0)

    async def _write_line(self, data: bytes) -> None:
        self.outbox.append(orjson.loads(data))

    async def run(self) -> list[dict]:
        await self.transport.serve()
        return self.outbox


# -- Code mapping ------------------------------------------------------


def test_each_subclass_carries_its_jsonrpc_code():
    """Every concrete error maps to the expected JSON-RPC code."""
    assert InvalidRequestError.code == _JSONRPC_INVALID_REQUEST
    assert MethodNotFoundError.code == _JSONRPC_METHOD_NOT_FOUND
    assert InvalidParamsError.code == _JSONRPC_INVALID_PARAMS
    assert InternalError.code == _JSONRPC_INTERNAL_ERROR
    assert ResourceNotFoundError.code == _JSONRPC_RESOURCE_NOT_FOUND
    assert AuthorizationError.code == _JSONRPC_FORBIDDEN


def test_base_defaults_to_internal_error_code():
    """The base error defaults to the internal-error code."""
    assert MCPError.code == _JSONRPC_INTERNAL_ERROR


def test_transport_errors_carry_invalid_request_code_and_http_status():
    """The HTTP-transport violations are `InvalidRequestError`s with a status."""
    assert issubclass(ProtocolVersionError, InvalidRequestError)
    assert issubclass(OriginNotAllowedError, InvalidRequestError)
    assert ProtocolVersionError.code == _JSONRPC_INVALID_REQUEST
    assert ProtocolVersionError("v").http_status == 400
    assert OriginNotAllowedError("o").http_status == 403


def test_subclasses_are_substitutable_for_the_base():
    """Every typed error is an `MCPError`, so one `except` catches all."""
    for cls in (
        InvalidRequestError,
        MethodNotFoundError,
        InvalidParamsError,
        InternalError,
        ResourceNotFoundError,
    ):
        assert issubclass(cls, MCPError)


# -- to_error rendering ------------------------------------------------


def test_to_error_renders_jsonrpc_error_object():
    """`to_error` produces a JSON-RPC 2.0 error response with the subclass code."""
    rendered = InvalidParamsError("bad arg").to_error(7)
    assert rendered == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": _JSONRPC_INVALID_PARAMS, "message": "bad arg"},
    }


def test_to_error_omits_data_when_none():
    """A plain error renders no `data` key."""
    rendered = InternalError("boom").to_error(1)
    assert "data" not in rendered["error"]


def test_to_error_includes_data_when_set():
    """An error carrying `data` renders it on the JSON-RPC error object."""
    rendered = MCPError("x", data={"hint": "y"}).to_error(2)
    assert rendered["error"]["data"] == {"hint": "y"}


def test_authorization_error_carries_required_scopes():
    """`AuthorizationError` renders its scopes under `data.requiredScopes`."""
    rendered = AuthorizationError(frozenset({"admin", "read"})).to_error(3)
    assert rendered["error"]["code"] == _JSONRPC_FORBIDDEN
    assert "insufficient_scope" in rendered["error"]["message"]
    assert rendered["error"]["data"]["requiredScopes"] == ["admin", "read"]


# -- In-band subtree ---------------------------------------------------


def test_stream_errors_are_private_in_band_subtree():
    """Stream-drain errors are `_InBandError`, themselves `MCPError`."""
    assert issubclass(_StreamTooLargeError, _InBandError)
    assert issubclass(_StreamTimeoutError, _InBandError)
    assert issubclass(_InBandError, MCPError)


def test_forbidden_error_uses_forbidden_code():
    """The private resource-read forbidden error carries the forbidden code."""
    assert _ForbiddenError("nope").to_error(4)["error"]["code"] == _JSONRPC_FORBIDDEN


# -- Dispatch integration ----------------------------------------------


async def test_handler_raised_invalid_params_surfaces_on_error_channel():
    """A handler raising `InvalidParamsError` is routed through `to_error`."""
    app = Veloce()

    @app.mcp_tool(description="reject")
    async def reject() -> str:
        raise InvalidParamsError("nope")

    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "reject"}})
    out = (await pipe.run())[0]
    assert out["error"]["code"] == _JSONRPC_INVALID_PARAMS
    assert "nope" in out["error"]["message"]


async def test_unknown_method_still_maps_to_method_not_found():
    """An unimplemented method returns the method-not-found code."""
    app = Veloce()
    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "does/not/exist", "params": {}})
    out = (await pipe.run())[0]
    assert out["error"]["code"] == _JSONRPC_METHOD_NOT_FOUND
