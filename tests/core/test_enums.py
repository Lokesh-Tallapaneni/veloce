"""String enums accept and compare equal to the plain strings they replace."""

from __future__ import annotations

import pytest

import veloce
from veloce import (
    CookiePrefix,
    Finding,
    MCPTransport,
    SameSite,
    ServerSessionMiddleware,
    TestClient,
    Veloce,
)
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


# ── cookie name prefixes ─────────────────────────────────────────────


def test_cookie_prefix_members_are_the_plain_strings():
    assert CookiePrefix.HOST == "host"
    assert str(CookiePrefix.SECURE) == "secure"


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [(CookiePrefix.HOST, "host"), ("host", "host"), (CookiePrefix.SECURE, "secure")],
)
def test_dump_cookie_accepts_the_enum_and_the_plain_string(prefix, expected):
    header = dump_cookie("sid", "x", secure=True, path="/", prefix=prefix)
    assert header.startswith("__Host-sid=" if expected == "host" else "__Secure-sid=")


def test_an_unknown_cookie_prefix_is_still_refused():
    with pytest.raises(ValueError, match="cookie prefix must be"):
        dump_cookie("sid", "x", secure=True, prefix="Host")  # type: ignore[arg-type]  # the refusal under test


def test_a_session_middleware_refuses_an_unknown_cookie_prefix_at_construction():
    with pytest.raises(ValueError, match="cookie_prefix must be"):
        ServerSessionMiddleware(cookie_prefix="Host", secure=True)  # type: ignore[arg-type]  # the refusal under test


def test_a_session_middleware_accepts_the_enum():
    middleware = ServerSessionMiddleware(cookie_prefix=CookiePrefix.SECURE, secure=True)
    assert middleware.cookie_prefix == "secure"


# ── audit severity ───────────────────────────────────────────────────


def test_a_finding_with_an_unknown_severity_is_refused_on_construction():
    with pytest.raises(ValueError, match="severity must be one of info, warning, error"):
        Finding("something", severity="eror")  # type: ignore[arg-type]  # the refusal under test


def test_at_least_refuses_an_unknown_threshold():
    with pytest.raises(ValueError, match="severity must be one of"):
        Finding("something", severity="error").at_least("fatal")  # type: ignore[arg-type]  # the refusal under test


def test_known_severities_still_rank():
    assert Finding("m", severity="error").at_least("warning")
    assert not Finding("m", severity="info").at_least("warning")
