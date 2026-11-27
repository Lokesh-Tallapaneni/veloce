"""`_meta` on a definition, plus a resource's declared size and annotations.

The protocol reserves `_meta` on every primitive for metadata it does not itself
define — which is how an extension carries its own data on a tool, resource or
prompt. Nothing could set it, so no extension could be expressed at all: MCP
Apps, which asks a client to render a tool's result as an interactive panel, is
the most visible thing that needs it, and versioning conventions are another.

A resource additionally has `size` and `annotations`. Both are declared rather
than measured — a listing must not have to read every resource to describe it.
"""

from __future__ import annotations

from veloce import APIRouter, Veloce
from veloce.contrib.mcp.registry import build_registry
from veloce.contrib.mcp.resources import build_resource_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession

UI = {"io.modelcontextprotocol/ui": {"resourceUri": "ui://widget/panel.html"}}


async def _listed(app: Veloce, method: str, key: str) -> dict[str, dict]:
    response = await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}, MCPSession()
    )
    entries = response["result"][key]
    # A resource entry carries both a uri and a name; the uri is what identifies it.
    return {entry.get("uri") or entry["name"]: entry for entry in entries}


# ── A tool ───────────────────────────────────────────────────────────


async def test_a_tool_publishes_the_meta_it_declares():
    app = Veloce(title="Meta", openapi_url=None)

    @app.mcp_tool(description="Render a panel", meta=UI)
    async def show_panel() -> int:
        return 1

    assert (await _listed(app, "tools/list", "tools"))["show_panel"]["_meta"] == UI


async def test_a_tool_declaring_none_publishes_no_meta_key():
    app = Veloce(title="Meta2", openapi_url=None)

    @app.mcp_tool(description="Plain")
    async def plain() -> int:
        return 1

    assert "_meta" not in (await _listed(app, "tools/list", "tools"))["plain"]


async def test_a_route_backed_tool_publishes_meta_too():
    """Both registration styles reach the same field."""
    app = Veloce(title="Meta3", openapi_url=None)

    @app.get("/panel", expose_as_mcp_tool=True, mcp_description="Route panel", mcp_meta=UI)
    async def panel() -> dict:
        return {}

    assert (await _listed(app, "tools/list", "tools"))["panel"]["_meta"] == UI


# ── A prompt ─────────────────────────────────────────────────────────


async def test_a_prompt_publishes_the_meta_it_declares():
    app = Veloce(title="Meta4", openapi_url=None)

    @app.mcp_prompt(description="Draft a note", meta={"team": "docs"})
    async def draft() -> str:
        return "hi"

    assert (await _listed(app, "prompts/list", "prompts"))["draft"]["_meta"] == {"team": "docs"}


# ── A resource ───────────────────────────────────────────────────────


def _resource_app(**route_kwargs) -> Veloce:
    app = Veloce(title="Meta5", openapi_url=None)

    @app.get(
        "/panel",
        expose_as_mcp_resource=True,
        mcp_resource_uri="ui://widget/panel.html",
        mcp_description="A panel",
        **route_kwargs,
    )
    async def panel() -> dict:
        return {}

    return app


async def test_a_resource_publishes_meta_size_and_annotations():
    app = _resource_app(
        mcp_meta=UI,
        mcp_resource_size=2048,
        mcp_resource_annotations={"audience": ["user"], "priority": 0.8},
    )
    entry = (await _listed(app, "resources/list", "resources"))["ui://widget/panel.html"]
    assert entry["_meta"] == UI
    assert entry["size"] == 2048
    assert entry["annotations"] == {"audience": ["user"], "priority": 0.8}


async def test_a_resource_declaring_nothing_publishes_none_of_them():
    entry = (await _listed(_resource_app(), "resources/list", "resources"))[
        "ui://widget/panel.html"
    ]
    assert "_meta" not in entry
    assert "size" not in entry
    assert "annotations" not in entry


async def test_a_size_of_zero_is_still_published():
    """An empty resource has a real size; `0` must not read as 'unset'."""
    entry = (await _listed(_resource_app(mcp_resource_size=0), "resources/list", "resources"))[
        "ui://widget/panel.html"
    ]
    assert entry["size"] == 0


async def test_the_declared_fields_survive_a_router_merge():

    router = APIRouter(prefix="/sub")

    @router.get(
        "/panel",
        expose_as_mcp_resource=True,
        mcp_resource_uri="ui://sub/panel.html",
        mcp_description="Nested",
        mcp_resource_size=64,
        mcp_meta={"nested": True},
    )
    async def panel() -> dict:
        return {}

    app = Veloce(title="Merged", openapi_url=None)
    app.include_router(router)

    resource = build_resource_registry(app).resources["ui://sub/panel.html"]
    assert resource.size == 64
    assert resource.meta == {"nested": True}


# ── The registry carries the declarations ────────────────────────────


def test_the_registry_records_what_was_declared():
    app = Veloce(title="Registry", openapi_url=None)

    @app.mcp_tool(description="Declared", meta=UI)
    async def declared() -> int:
        return 1

    @app.mcp_tool(description="Undeclared")
    async def undeclared() -> int:
        return 1

    tools = build_registry(app).tools
    assert tools["declared"].meta == UI
    assert tools["undeclared"].meta is None


async def test_meta_travels_beside_the_other_listing_fields():
    """Adding `_meta` must not displace what a listing already carried."""
    app = Veloce(title="Together", openapi_url=None)

    @app.mcp_tool(
        description="Everything at once",
        meta=UI,
        annotations={"readOnlyHint": True},
        tags=["ui"],
    )
    async def everything(a: int) -> int:
        return a

    entry = (await _listed(app, "tools/list", "tools"))["everything"]
    assert entry["_meta"] == UI
    assert entry["annotations"] == {"readOnlyHint": True}
    assert entry["description"] == "Everything at once"
    assert entry["inputSchema"]["properties"]["a"] == {"type": "integer"}
