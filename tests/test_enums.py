"""String enums accept and compare equal to the plain strings they replace."""

from __future__ import annotations

import pytest

import veloce
from veloce import MCPTransport, SameSite, TestClient, Veloce
from veloce.http.cookies import dump_cookie


def test_members_are_strings_and_format_as_their_value():
    assert MCPTransport.HTTP == "http"
    assert isinstance(SameSite.LAX, str)
    assert f"{MCPTransport.SSE}" == "sse"
    assert str(SameSite.NONE) == "None"


def test_mount_mcp_accepts_the_enum_and_the_plain_string():
    def probe(value):
        app = Veloce()
        app.mount_mcp(value, sessions=True)
        reply = TestClient(app).post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        return reply.status_code, reply.text

    assert probe(MCPTransport.HTTP) == probe("http")


def test_mount_mcp_still_rejects_an_unknown_transport():
    with pytest.raises(ValueError, match="Unsupported MCP transport 'ws'"):
        Veloce().mount_mcp("ws")


def test_dump_cookie_takes_the_enum_or_any_case_string():
    from_enum = dump_cookie("a", "b", samesite=SameSite.STRICT)
    assert from_enum == dump_cookie("a", "b", samesite="strict")
    assert "SameSite=Strict" in from_enum


def test_dump_cookie_still_rejects_an_unknown_value():
    with pytest.raises(ValueError, match="samesite must be"):
        dump_cookie("a", "b", samesite="sometimes")


def test_enums_are_public():
    assert {"MCPTransport", "SameSite"} <= set(veloce.__all__)
