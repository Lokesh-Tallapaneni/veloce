"""What MCP exposure actually gates, pinned against what the docs claim.

`registry.py` said the safety policy was "a mutating route is never
auto-exposed". A reader auditing an MCP mount would take that as a verb-based
gate: `GET` routes can become tools, mutating ones cannot.

There is no such gate. `safety.py` says so in as many words - exposure is
"regardless of HTTP verb" - and running it agrees: a `DELETE` route carrying
`expose_as_mcp_tool=True` is a tool. The claim was true only in the vacuous
sense that *nothing* is auto-exposed, and in a security context a vacuous
assurance reads as a real one.

The docstring now states the real policy. These tests are what stop the two
drifting again: the policy is default-closed and verb-blind, and both halves
are asserted here.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp import MCPServer, registry, safety


def _server_with(method: str) -> MCPServer:
    app = Veloce(title="S", version="1.0.0", openapi_url=None)
    decorator = getattr(app, method)

    @decorator("/thing", expose_as_mcp_tool=True, mcp_description="Act on a thing")
    async def act() -> dict:
        return {"ok": True}

    return MCPServer(app)


# ── default-closed: nothing is exposed unless asked ──────────────────


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
def test_a_route_is_not_exposed_by_default(method):
    """The half of the policy that is real, and the one that matters."""
    app = Veloce(title="S", version="1.0.0", openapi_url=None)

    @getattr(app, method)("/thing")
    async def act() -> dict:
        return {"ok": True}

    assert MCPServer(app).registry.tools == {}


# ── and exposure is verb-blind, which the docstring denied ───────────


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
def test_an_exposed_route_becomes_a_tool_whatever_its_method(method):
    """The defect: `registry.py` implied a mutating route could not."""
    assert "act" in _server_with(method).registry.tools


def test_a_delete_route_is_exposed_when_asked():
    """Stated on its own because it is the case the old wording denied."""
    assert "act" in _server_with("delete").registry.tools


# ── the docs and the behaviour agree ─────────────────────────────────


def test_neither_module_asserts_a_verb_based_gate():
    """The two doors: a docstring asserting a security control that does not
    exist is worse than no docstring.

    Neither module may say a mutating route is protected from exposure by its
    verb, because nothing implements that. Not even to quote and disown it -
    the phrase is absent, so there is no sentence for a later edit to turn back
    into a claim.
    """

    for module in (registry, safety):
        text = (module.__doc__ or "").lower()
        assert "never auto-exposed" not in text, (
            f"{module.__name__} names a verb-based gate that does not exist"
        )


def test_the_registry_docstring_says_exposure_is_verb_blind():

    text = (registry.__doc__ or "").lower()
    assert "whatever its http method" in text or "regardless of http verb" in text


def test_the_description_rule_is_the_one_that_is_enforced():
    """What the registry really gates, so the corrected docstring is not vacuous."""
    app = Veloce(title="S", version="1.0.0", openapi_url=None)

    @app.get("/thing", expose_as_mcp_tool=True)
    async def act() -> dict:
        return {"ok": True}

    with pytest.raises(ValueError, match="missing a description"):
        MCPServer(app)
