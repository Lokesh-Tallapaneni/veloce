"""A tool says what it does, whichever way it was registered.

`ToolAnnotations` are the hints a client shows a human before letting a model
call something — read-only, destructive, idempotent, open-world. A route-backed
tool derives them from its HTTP verb. A tool registered with `@app.mcp_tool` has
no verb to derive from, so it declares them; before, it could not say anything at
all, and a destructive tool looked exactly like a harmless one.

Declaring nothing stays meaningful: the spec's own defaults are the cautious
reading (destructive, open-world), so an undeclared tool is not assumed safe.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp.registry import build_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


async def _listed(app: Veloce) -> dict[str, dict]:
    response = await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, MCPSession()
    )
    return {entry["name"]: entry for entry in response["result"]["tools"]}


# ── A pure tool can state its hints ──────────────────────────────────


async def test_a_pure_tool_publishes_the_hints_it_declares():
    app = Veloce(title="Hints", openapi_url=None)

    @app.mcp_tool(
        description="Delete a widget",
        annotations={"destructiveHint": True, "idempotentHint": True, "readOnlyHint": False},
    )
    async def delete_widget(widget_id: str) -> str:
        return "gone"

    assert (await _listed(app))["delete_widget"]["annotations"] == {
        "destructiveHint": True,
        "idempotentHint": True,
        "readOnlyHint": False,
    }


async def test_a_read_only_pure_tool_can_say_so():
    app = Veloce(title="Hints2", openapi_url=None)

    @app.mcp_tool(description="Read a widget", annotations={"readOnlyHint": True})
    async def read_widget(widget_id: str) -> str:
        return "w"

    assert (await _listed(app))["read_widget"]["annotations"] == {"readOnlyHint": True}


async def test_a_declared_title_reaches_the_listing():
    app = Veloce(title="Hints3", openapi_url=None)

    @app.mcp_tool(description="Do it", annotations={"title": "Do The Thing"})
    async def do_it() -> int:
        return 1

    assert (await _listed(app))["do_it"]["annotations"]["title"] == "Do The Thing"


async def test_declaring_nothing_publishes_no_annotations():
    """The spec's defaults already read cautiously; inventing hints would not."""
    app = Veloce(title="Hints4", openapi_url=None)

    @app.mcp_tool(description="Undeclared")
    async def plain() -> int:
        return 1

    assert "annotations" not in (await _listed(app))["plain"]


@pytest.mark.parametrize("empty", [None, {}])
async def test_an_empty_declaration_is_the_same_as_none(empty):
    app = Veloce(title="Hints5", openapi_url=None)

    @app.mcp_tool(description="Empty", annotations=empty)
    async def empty_tool() -> int:
        return 1

    assert "annotations" not in (await _listed(app))["empty_tool"]


# ── A hint no client reads is refused ────────────────────────────────


def test_an_unknown_hint_is_refused_at_the_decorator():
    """Reported against the decorator that wrote it, not later at mount time."""
    app = Veloce(title="Bad", openapi_url=None)

    with pytest.raises(ValueError, match="readonlyHint"):

        @app.mcp_tool(description="Typo", annotations={"readonlyHint": True})
        async def typo() -> int:
            return 1


def test_the_refusal_names_the_hints_that_are_valid():
    app = Veloce(title="Bad2", openapi_url=None)

    with pytest.raises(ValueError, match="destructiveHint"):

        @app.mcp_tool(description="Typo", annotations={"nonsense": True})
        async def typo() -> int:
            return 1


def test_the_validator_accepts_every_hint_the_spec_defines():
    from veloce.contrib.mcp.safety import TOOL_ANNOTATION_HINTS, validate_tool_annotations

    every = dict.fromkeys(TOOL_ANNOTATION_HINTS, True)
    assert validate_tool_annotations(every) == every


def test_the_validator_copies_rather_than_aliasing():
    """A caller mutating its dict afterwards must not change the registered tool."""
    from veloce.contrib.mcp.safety import validate_tool_annotations

    declared = {"readOnlyHint": True}
    stored = validate_tool_annotations(declared)
    declared["readOnlyHint"] = False
    assert stored == {"readOnlyHint": True}


# ── Route-backed tools keep deriving, and may correct ────────────────


async def test_a_route_backed_tool_still_derives_from_its_verb():
    app = Veloce(title="Routes", openapi_url=None)

    @app.delete("/w/{i}", expose_as_mcp_tool=True, mcp_description="Route-backed delete")
    async def route_delete(i: str) -> dict:
        return {}

    assert (await _listed(app))["route_delete"]["annotations"] == {
        "readOnlyHint": False,
        "idempotentHint": True,
        "destructiveHint": True,
    }


async def test_both_registration_styles_can_describe_the_same_operation():
    """The finding: two identical deletes, one of which said nothing."""
    app = Veloce(title="Both", openapi_url=None)

    @app.mcp_tool(description="Delete (pure)", annotations={"destructiveHint": True})
    async def delete_pure(widget_id: str) -> str:
        return "gone"

    @app.delete("/w/{i}", expose_as_mcp_tool=True, mcp_description="Delete (route)")
    async def delete_route(i: str) -> dict:
        return {}

    listed = await _listed(app)
    assert listed["delete_pure"]["annotations"]["destructiveHint"] is True
    assert listed["delete_route"]["annotations"]["destructiveHint"] is True


# ── The registry carries what was declared ───────────────────────────


def test_the_registry_records_the_declaration():
    app = Veloce(title="Registry", openapi_url=None)

    @app.mcp_tool(description="Declared", annotations={"readOnlyHint": True})
    async def declared() -> int:
        return 1

    @app.mcp_tool(description="Undeclared")
    async def undeclared() -> int:
        return 1

    tools = build_registry(app).tools
    assert tools["declared"].annotations == {"readOnlyHint": True}
    assert tools["undeclared"].annotations is None
