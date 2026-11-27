"""`Security(scopes=...)` takes a sequence of scopes, not a string."""

from __future__ import annotations

import pytest

from veloce import Security, Veloce
from veloce._handler_plan import build_plan
from veloce.contrib.mcp.auth import MCPAuth
from veloce.contrib.mcp.authorization import MCPAuthorizationServer


def _authz(request):
    return "ok"


def _scope_lists(handler) -> list[list[str]]:
    return [
        slot.target_type
        for slot in build_plan(handler).slots
        if isinstance(getattr(slot, "target_type", None), list)
    ]


def test_a_list_of_scopes_is_recorded_whole():
    """POSITIVE: the declared scopes reach the plan unchanged."""

    def handler(request, user=Security(_authz, scopes=["read", "write"])):
        return user

    assert ["read", "write"] in _scope_lists(handler)


def test_a_tuple_of_scopes_is_accepted():
    """POSITIVE: any sequence works; only str/bytes are the trap."""

    def handler(request, user=Security(_authz, scopes=("read",))):
        return user

    assert ["read"] in _scope_lists(handler)


@pytest.mark.parametrize("bad", ["read", b"read"])
def test_a_bare_string_or_bytes_scope_is_refused(bad):
    """NEGATIVE: `scopes="read"` became ['r','e','a','d']; bytes iterate to ints.

    One branch, so one test: both spellings reach the same
    `isinstance(scopes, (str, bytes))` guard.
    """
    with pytest.raises(TypeError, match="sequence of scopes"):
        Security(_authz, scopes=bad)


def test_no_scopes_is_still_valid():
    """POSITIVE: `Security()` without scopes is the common form.

    `__init__` normalises a missing value to `[]`, so that is what is asserted.
    """
    assert Security(_authz).scopes == []


# ── the same split exists on every other scope entry point ──


@pytest.mark.parametrize("bad", ["admin", b"admin"])
def test_mcp_tool_refuses_a_bare_string_scope(bad):
    """NEGATIVE: `scopes="admin"` used to become a five-character frozenset."""
    app = Veloce()
    with pytest.raises(TypeError, match="sequence of scopes"):

        @app.mcp_tool(name="t", description="a tool", scopes=bad)
        async def t(request):
            return {}


@pytest.mark.parametrize("bad", ["admin", b"admin"])
def test_mcp_prompt_refuses_a_bare_string_scope(bad):
    """NEGATIVE: the prompt decorator splits identically."""
    app = Veloce()
    with pytest.raises(TypeError, match="sequence of scopes"):

        @app.mcp_prompt(name="p", description="a prompt", scopes=bad)
        async def p(request):
            return {}


def test_mcp_auth_refuses_a_bare_string_required_scope():
    """NEGATIVE: every request would be checked against character scopes."""
    with pytest.raises(TypeError, match="sequence of scopes"):
        MCPAuth(
            verify=lambda token: None,
            required_scopes="admin",
            resource_server_url="https://api.example.com/mcp",
            authorization_servers=["https://auth.example.com"],
        )


def test_mcp_auth_refuses_a_bare_string_supported_scope():
    """NEGATIVE: this one does not fail closed - it publishes the nonsense.

    `scopes_supported` is advertised in the protected-resource metadata, so a
    bare string is served to every client that reads it.
    """
    with pytest.raises(TypeError, match="sequence of scopes"):
        MCPAuth(
            verify=lambda token: None,
            scopes_supported="read",
            resource_server_url="https://api.example.com/mcp",
            authorization_servers=["https://auth.example.com"],
        )


def test_authorization_server_refuses_a_bare_string_supported_scope():
    """NEGATIVE: the same advertised-metadata split on the server class."""
    with pytest.raises(TypeError, match="sequence of scopes"):
        MCPAuthorizationServer(
            issuer="https://issuer.example",
            authenticate=lambda *args: None,
            scopes_supported="admin",
        )


def test_mcp_tool_accepts_a_list_of_scopes():
    """POSITIVE: the guard must not refuse the correct spelling."""
    app = Veloce()

    @app.mcp_tool(name="t", description="a tool", scopes=["read", "write"])
    async def t(request):
        return {}

    assert app._mcp_tools[-1].scopes == frozenset({"read", "write"})


def test_mcp_auth_accepts_sequences():
    """POSITIVE: tuples and lists pass through unchanged."""
    auth = MCPAuth(
        verify=lambda token: None,
        required_scopes=["mcp:tools"],
        scopes_supported=("read",),
        resource_server_url="https://api.example.com/mcp",
        authorization_servers=["https://auth.example.com"],
    )
    assert auth.required_scopes == frozenset({"mcp:tools"})
    assert auth.scopes_supported == ("read",)
