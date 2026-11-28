"""Every subsystem's exceptions share a root that derives from `VeloceError`."""

from __future__ import annotations

import pytest

import veloce
from veloce import VeloceError, WebSocketDisconnect, WebSocketException
from veloce._scaffold import ScaffoldError
from veloce.contrib.mcp.errors import MCPError, ToolError
from veloce.contrib.mcp.transports.stdio import MCPRequestError
from veloce.exceptions import WebSocketRequestValidationError
from veloce.security.jwt import ExpiredSignatureError, JWTError
from veloce.signing import BadData, BadSignature, BadTimeSignature


@pytest.mark.parametrize(
    "exc",
    [WebSocketDisconnect(), WebSocketException(1008, "x"), WebSocketRequestValidationError([])],
)
def test_websocket_errors_share_a_root(exc):
    with pytest.raises(veloce.WebSocketError):
        raise exc


def test_websocket_validation_error_keeps_its_http_lineage():
    assert issubclass(WebSocketRequestValidationError, veloce.RequestValidationError)
    assert issubclass(WebSocketRequestValidationError, veloce.WebSocketError)


@pytest.mark.parametrize(
    "cls",
    [
        veloce.WebSocketError,
        MCPError,
        ToolError,
        MCPRequestError,
        ScaffoldError,
        JWTError,
        ExpiredSignatureError,
        BadSignature,
        BadTimeSignature,
        BadData,
    ],
)
def test_every_subsystem_error_is_a_veloce_error(cls):
    assert issubclass(cls, VeloceError)


def test_mcp_error_still_renders_a_json_rpc_error():
    assert MCPError("boom", data=1).to_error(7)["error"]["message"] == "boom"


# ── subsystem roots and the backend refusal ──────────────────────────


@pytest.mark.parametrize("name", ["CacheError", "SessionError", "RateLimitError", "BackendError"])
def test_the_subsystem_roots_derive_from_veloce_error(name):
    cls = getattr(veloce, name)
    assert issubclass(cls, VeloceError)
    assert name in veloce.__all__


def test_a_subsystem_error_is_caught_by_its_own_root_only():
    class StoreDown(veloce.SessionError):
        pass

    with pytest.raises(veloce.SessionError):
        raise StoreDown("down")
    assert not issubclass(StoreDown, veloce.CacheError)


def test_a_backend_refusal_is_also_a_type_error():
    """Existing `except TypeError` handling keeps catching it."""

    class Empty:
        pass

    with pytest.raises(TypeError) as excinfo:
        veloce.cached(Empty(), ttl=5)
    assert isinstance(excinfo.value, veloce.BackendError)
    assert isinstance(excinfo.value, VeloceError)
