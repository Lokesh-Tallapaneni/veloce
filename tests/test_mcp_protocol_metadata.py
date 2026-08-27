"""Protocol version, ping, tool annotations, schema dialect, and serverInfo.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import asyncio

from tests._mcp import Pipe
from tests._mcp_shared import (
    PublicUser,
    _initialize,
    _list_tools,
    _server,
)
from veloce import (
    Veloce,
)

# -- Protocol version + ping ------------------------------------------


def test_initialize_echoes_supported_protocol_version():
    """A client's requested version is echoed back when the server supports it."""
    app = Veloce(openapi_url=None)
    resp = _initialize(app, {"protocolVersion": "2025-06-18"})
    assert resp["result"]["protocolVersion"] == "2025-06-18"


def test_initialize_falls_back_to_latest_for_unknown_version():
    """An unrecognised requested version yields the server's latest supported."""
    from veloce.contrib.mcp.server import LATEST_PROTOCOL_VERSION

    app = Veloce(openapi_url=None)
    resp = _initialize(app, {"protocolVersion": "1999-01-01"})
    assert resp["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


def test_initialize_without_version_returns_latest():
    """An `initialize` with no `protocolVersion` returns the latest supported."""
    from veloce.contrib.mcp.server import LATEST_PROTOCOL_VERSION

    app = Veloce(openapi_url=None)
    resp = _initialize(app, {})
    assert resp["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


def test_ping_returns_empty_result():
    """`ping` is answered with an empty result object, not method-not-found."""
    app = Veloce(openapi_url=None)
    pipe = Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 9, "method": "ping", "params": {}})
    out = asyncio.run(pipe.run())[0]
    assert "error" not in out
    assert out["result"] == {}


# -- Tool annotations + title -----------------------------------------


def test_get_tool_is_read_only_and_idempotent():
    app = Veloce(openapi_url=None)

    @app.get("/items", expose_as_mcp_tool=True, mcp_description="List items")
    async def list_items() -> dict:
        return {"items": []}

    ann = _list_tools(app)["list_items"]["annotations"]
    assert ann["readOnlyHint"] is True
    assert ann["idempotentHint"] is True
    assert ann["destructiveHint"] is False


def test_post_tool_is_additive_not_idempotent():
    app = Veloce(openapi_url=None)

    @app.post("/items", expose_as_mcp_tool=True, mcp_description="Create item")
    async def create_item() -> dict:
        return {"ok": True}

    ann = _list_tools(app)["create_item"]["annotations"]
    assert ann["readOnlyHint"] is False
    assert ann["idempotentHint"] is False
    assert ann["destructiveHint"] is False


def test_query_tool_is_read_only_and_idempotent():
    # QUERY is safe and idempotent (RFC 10008), so its tool annotations match
    # GET rather than POST.
    app = Veloce(openapi_url=None)

    @app.query("/search", expose_as_mcp_tool=True, mcp_description="Search items")
    async def search() -> dict:
        return {"items": []}

    ann = _list_tools(app)["search"]["annotations"]
    assert ann["readOnlyHint"] is True
    assert ann["idempotentHint"] is True
    assert ann["destructiveHint"] is False


def test_delete_tool_is_destructive_and_idempotent():
    app = Veloce(openapi_url=None)

    @app.delete("/items/{n}", expose_as_mcp_tool=True, mcp_description="Delete item")
    async def delete_item(n: int) -> dict:
        return {"deleted": n}

    ann = _list_tools(app)["delete_item"]["annotations"]
    assert ann["readOnlyHint"] is False
    assert ann["idempotentHint"] is True
    assert ann["destructiveHint"] is True


def test_pure_tool_has_no_annotations():
    """A pure `@app.mcp_tool` has no HTTP verb, so it carries no annotations."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    assert "annotations" not in _list_tools(app)["add"]


def test_tool_title_from_route_summary():
    app = Veloce(openapi_url=None)

    @app.get(
        "/health",
        summary="Health probe",
        expose_as_mcp_tool=True,
        mcp_description="Check service health",
    )
    async def health() -> dict:
        return {"ok": True}

    assert _list_tools(app)["health"]["title"] == "Health probe"


def test_tool_without_summary_has_no_title():
    app = Veloce(openapi_url=None)

    @app.get("/health", expose_as_mcp_tool=True, mcp_description="Check health")
    async def health() -> dict:
        return {"ok": True}

    assert "title" not in _list_tools(app)["health"]


def test_read_only_tool_is_closed_world():
    """A read-only route operates on the server's own data, so openWorldHint=False."""
    app = Veloce(openapi_url=None)

    @app.get("/items", expose_as_mcp_tool=True, mcp_description="List items")
    async def list_items() -> dict:
        return {"items": []}

    ann = _list_tools(app)["list_items"]["annotations"]
    assert ann["openWorldHint"] is False


def test_mutating_tool_omits_open_world_hint():
    """A mutating route leaves openWorldHint to the spec's open-world default."""
    app = Veloce(openapi_url=None)

    @app.post("/items", expose_as_mcp_tool=True, mcp_description="Create item")
    async def create_item() -> dict:
        return {"ok": True}

    assert "openWorldHint" not in _list_tools(app)["create_item"]["annotations"]


def test_annotation_carries_title():
    """The route summary appears as annotations.title alongside the top-level title."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/health",
        summary="Health probe",
        expose_as_mcp_tool=True,
        mcp_description="Check health",
    )
    async def health() -> dict:
        return {"ok": True}

    ann = _list_tools(app)["health"]["annotations"]
    assert ann["title"] == "Health probe"


def test_pure_tool_without_title_has_no_annotations():
    """A pure tool with no verb and no title still carries no annotation block."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    assert "annotations" not in _list_tools(app)["add"]


# -- Schema dialect ---------------------------------------------------


def test_input_schema_declares_2020_12_dialect():
    """Every tool input schema declares the JSON Schema 2020-12 dialect."""
    from veloce.contrib.mcp.plan_bridge import JSON_SCHEMA_DIALECT

    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    assert _list_tools(app)["add"]["inputSchema"]["$schema"] == JSON_SCHEMA_DIALECT


def test_output_schema_declares_2020_12_dialect():
    """A declared output schema also declares the 2020-12 dialect."""
    from veloce.contrib.mcp.plan_bridge import JSON_SCHEMA_DIALECT

    app = Veloce(openapi_url=None)

    @app.get(
        "/me",
        response_model=PublicUser,
        expose_as_mcp_tool=True,
        mcp_description="Current user",
    )
    async def me() -> dict:
        return {"id": 1, "name": "ada"}

    assert _list_tools(app)["me"]["outputSchema"]["$schema"] == JSON_SCHEMA_DIALECT


# -- initialize instructions + serverInfo title -----------------------


def test_initialize_emits_instructions_from_description():
    """The app description becomes the initialize `instructions` field."""
    app = Veloce(openapi_url=None, description="Use list_items before create_item.")
    result = _initialize(app, {})["result"]
    assert result["instructions"] == "Use list_items before create_item."


def test_initialize_instructions_fall_back_to_summary():
    """With no description, the one-line summary becomes the instructions."""
    app = Veloce(openapi_url=None, summary="A small task API.")
    result = _initialize(app, {})["result"]
    assert result["instructions"] == "A small task API."


def test_initialize_omits_instructions_when_unset():
    """Neither description nor summary set: no instructions field is emitted."""
    app = Veloce(openapi_url=None)
    assert "instructions" not in _initialize(app, {})["result"]


def test_initialize_serverinfo_carries_title():
    """The app title is the human-facing serverInfo.title."""
    app = Veloce(openapi_url=None, title="Task Service")
    server_info = _initialize(app, {})["result"]["serverInfo"]
    assert server_info["title"] == "Task Service"
    assert server_info["name"] == "Task Service"
