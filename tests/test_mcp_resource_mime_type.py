"""The media type a resource listing advertises.

A listing carried only uri/name/description, so a client had to read a resource
before it knew what kind of content it would get. A declared media type is now
published - declared, never inferred: the response class is chosen from the
handler's actual return value, so a type guessed from its annotation could
contradict what a read returns, and a listing that disagrees with the read is
worse than one that stays silent.
"""

from __future__ import annotations

from veloce import APIRouter, HTMLResponse, PlainTextResponse, Veloce
from veloce.contrib.mcp.resources import build_resource_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


def _app() -> Veloce:
    app = Veloce(title="MimeProbe", version="1.0.0", openapi_url=None)

    @app.get(
        "/declared",
        expose_as_mcp_resource=True,
        mcp_resource_uri="res://declared",
        mcp_description="Explicitly declared",
        mcp_resource_mime_type="text/markdown",
    )
    async def declared() -> dict:
        return {"body": "# Title"}

    @app.get(
        "/html",
        expose_as_mcp_resource=True,
        mcp_resource_uri="res://html",
        mcp_description="Declared by its response class",
        response_class=HTMLResponse,
    )
    async def html() -> str:
        return "<p>hi</p>"

    @app.get(
        "/plain",
        expose_as_mcp_resource=True,
        mcp_resource_uri="res://plain",
        mcp_description="Plain text response class",
        response_class=PlainTextResponse,
    )
    async def plain() -> str:
        return "words"

    @app.get(
        "/undeclared",
        expose_as_mcp_resource=True,
        mcp_resource_uri="res://undeclared",
        mcp_description="Nothing declared",
    )
    async def undeclared() -> dict:
        return {"x": 1}

    @app.get(
        "/tpl/{item_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="res://tpl/{item_id}",
        mcp_description="A template",
        mcp_resource_mime_type="application/json",
    )
    async def tpl(item_id: str) -> dict:
        return {"item_id": item_id}

    return app


async def _listing(method: str, key: str) -> dict[str, dict]:
    response = await MCPServer(_app()).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}, MCPSession()
    )
    entries = response["result"][key]
    return {e.get("uri") or e["uriTemplate"]: e for e in entries}


async def _read_mime(uri: str) -> str:
    response = await MCPServer(_app()).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}},
        MCPSession(),
    )
    return response["result"]["contents"][0]["mimeType"]


# ── What is published ────────────────────────────────────────────────


async def test_an_explicitly_declared_media_type_is_published():
    entries = await _listing("resources/list", "resources")
    assert entries["res://declared"]["mimeType"] == "text/markdown"


async def test_a_response_class_supplies_the_media_type():
    entries = await _listing("resources/list", "resources")
    assert entries["res://html"]["mimeType"] == "text/html"


async def test_the_charset_parameter_is_not_advertised():
    """A read reports the bare type, so the listing must not carry parameters."""
    entries = await _listing("resources/list", "resources")
    assert ";" not in entries["res://plain"]["mimeType"]
    assert entries["res://plain"]["mimeType"] == "text/plain"


async def test_nothing_is_advertised_when_nothing_is_declared():
    """Silence beats a guess that could contradict the read."""
    entries = await _listing("resources/list", "resources")
    assert "mimeType" not in entries["res://undeclared"]


async def test_a_template_listing_carries_it_too():
    entries = await _listing("resources/templates/list", "resourceTemplates")
    assert entries["res://tpl/{item_id}"]["mimeType"] == "application/json"


# ── The listing agrees with the read ─────────────────────────────────


async def test_the_advertised_type_matches_what_a_read_returns():
    """The invariant: a listing that contradicts the read is worse than silence.

    A declared type is authoritative for both, so the two cannot drift apart -
    including when the handler's own response would have reported another type.
    """
    entries = await _listing("resources/list", "resources")
    for uri, entry in entries.items():
        advertised = entry.get("mimeType")
        if advertised is not None:
            assert advertised == await _read_mime(uri), uri


async def test_a_declared_type_wins_over_the_response_the_handler_produced():
    """`res://declared` returns a mapping, which would otherwise read as JSON."""
    assert await _read_mime("res://declared") == "text/markdown"


# ── Nothing else about the entry changed ─────────────────────────────


async def test_the_other_entry_fields_are_untouched():
    entries = await _listing("resources/list", "resources")
    entry = entries["res://declared"]
    assert entry["name"] and entry["description"] == "Explicitly declared"


async def test_a_route_without_the_parameter_still_registers():
    entries = await _listing("resources/list", "resources")
    assert "res://undeclared" in entries


def test_the_declaration_survives_a_router_merge():
    """`include_router` rebuilds each route; the declaration must be carried."""

    router = APIRouter(prefix="/sub")

    @router.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="res://sub/doc",
        mcp_description="Nested",
        mcp_resource_mime_type="text/markdown",
    )
    async def doc() -> dict:
        return {}

    app = Veloce(title="Merged", openapi_url=None)
    app.include_router(router)
    resource = build_resource_registry(app).resources["res://sub/doc"]
    assert resource.tool.route_info.mcp_resource_mime_type == "text/markdown"
