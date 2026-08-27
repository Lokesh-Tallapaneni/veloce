"""MCP error hierarchy - JSON-RPC code mapping, `to_error` rendering, dispatch."""

from __future__ import annotations

import pytest

from tests._mcp import Pipe
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
from veloce.contrib.mcp import errors as errors_module
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


def _server(app: Veloce) -> MCPServer:
    return MCPServer(app)


# -- Code mapping ------------------------------------------------------


#: The numbers themselves, spelled out. Comparing each class attribute against
#: the constant `errors.py` assigns to it restates the module: rename the
#: constant and both sides move together, and a wrong number reaches the wire
#: with the test still green. These are the protocol (JSON-RPC 2.0 Sec. 5.1 for
#: the reserved range, the MCP spec for the rest), so they are written as
#: literals here on purpose.
WIRE_CODES = {
    "InvalidRequestError": -32600,
    "MethodNotFoundError": -32601,
    "InvalidParamsError": -32602,
    "InternalError": -32603,
    "ResourceNotFoundError": -32002,
    "AuthorizationError": -32003,
}


@pytest.mark.parametrize(("name", "code"), sorted(WIRE_CODES.items()))
def test_each_subclass_carries_its_jsonrpc_code(name, code):
    """Every concrete error maps to the number the protocol reserves for it."""
    assert getattr(errors_module, name).code == code


def test_the_named_constants_agree_with_the_wire_numbers():
    """The two spellings must not drift, in either direction."""
    assert WIRE_CODES["InvalidRequestError"] == _JSONRPC_INVALID_REQUEST
    assert WIRE_CODES["MethodNotFoundError"] == _JSONRPC_METHOD_NOT_FOUND
    assert WIRE_CODES["InvalidParamsError"] == _JSONRPC_INVALID_PARAMS
    assert WIRE_CODES["InternalError"] == _JSONRPC_INTERNAL_ERROR
    assert WIRE_CODES["ResourceNotFoundError"] == _JSONRPC_RESOURCE_NOT_FOUND
    assert WIRE_CODES["AuthorizationError"] == _JSONRPC_FORBIDDEN


def test_base_defaults_to_internal_error_code():
    """The base error defaults to the internal-error code."""
    assert MCPError.code == -32603


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

    pipe = Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "reject"}})
    out = (await pipe.run())[0]
    assert out["error"]["code"] == _JSONRPC_INVALID_PARAMS
    assert "nope" in out["error"]["message"]


async def test_unknown_method_still_maps_to_method_not_found():
    """An unimplemented method returns the method-not-found code."""
    app = Veloce()
    pipe = Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "does/not/exist", "params": {}})
    out = (await pipe.run())[0]
    assert out["error"]["code"] == _JSONRPC_METHOD_NOT_FOUND
