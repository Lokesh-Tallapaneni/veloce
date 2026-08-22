"""Publishing one tool while keeping its earlier versions callable.

A tool's contract changes, and the old shape has callers. Registering both meant
two names in the catalogue - two entries an agent reads and has to choose
between - because a duplicate name was refused outright.

A registration may now declare a `version`. Same-name registrations differing in
version are all kept: the highest is the one listed and the one a call naming no
version reaches, and any of them answers a call naming its own. The spec has no
version field, so both the published version and the requested one travel in
`_meta` under one framework-namespaced key.
"""

from __future__ import annotations

import orjson
import pytest

from veloce import Veloce
from veloce.contrib.mcp.registry import build_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


def _app() -> Veloce:
    app = Veloce(title="Versioned", version="1.0.0", openapi_url=None)

    @app.mcp_tool(name="search", description="Search (v1)", version="1.0")
    async def search_v1(q: str) -> dict:
        return {"v": 1, "q": q}

    @app.mcp_tool(name="search", description="Search (v2)", version="2.0")
    async def search_v2(query: str) -> dict:
        return {"v": 2, "query": query}

    @app.mcp_tool(name="search", description="Search (v10)", version="10.0")
    async def search_v10(query: str) -> dict:
        return {"v": 10, "query": query}

    return app


async def _list(app: Veloce) -> list[dict]:
    response = await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, MCPSession()
    )
    listed: list[dict] = response["result"]["tools"]
    return listed


async def _call(app: Veloce, arguments: dict, version: str | None = None) -> dict:
    params: dict = {"name": "search", "arguments": arguments}
    if version is not None:
        params["_meta"] = {"veloce": {"version": version}}
    response = await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}, MCPSession()
    )
    return response


def _payload(response: dict) -> dict:
    result = response["result"]
    assert not result.get("isError"), result["content"][0]["text"]
    parsed: dict = orjson.loads(result["content"][0]["text"])
    return parsed


# ── One entry in the catalogue ───────────────────────────────────────


async def test_only_one_entry_is_listed_for_the_name():
    """The point: three versions must not cost the agent three descriptions."""
    assert [tool["name"] for tool in await _list(_app())] == ["search"]


async def test_the_listed_entry_is_the_highest_version():
    assert (await _list(_app()))[0]["description"] == "Search (v10)"


async def test_ten_sorts_above_two():
    """A plain string sort gets this backwards."""
    assert (await _list(_app()))[0]["_meta"]["veloce"]["version"] == "10.0"


async def test_the_listed_schema_is_the_listed_version_s():
    listed = (await _list(_app()))[0]
    assert set(listed["inputSchema"]["properties"]) == {"query"}


async def test_every_registered_version_is_published():
    assert (await _list(_app()))[0]["_meta"]["veloce"]["versions"] == ["1.0", "2.0", "10.0"]


# ── Reaching a version ───────────────────────────────────────────────


async def test_a_call_naming_no_version_reaches_the_listed_one():
    assert _payload(await _call(_app(), {"query": "cats"}))["v"] == 10


async def test_a_call_naming_a_version_reaches_that_one():
    assert _payload(await _call(_app(), {"q": "cats"}, version="1.0"))["v"] == 1


async def test_the_addressed_version_takes_its_own_arguments():
    assert _payload(await _call(_app(), {"query": "cats"}, version="2.0")) == {
        "v": 2,
        "query": "cats",
    }


async def test_an_unknown_version_is_refused_by_name_and_version():
    response = await _call(_app(), {"q": "cats"}, version="9.9")
    assert response["error"]["message"] == "Unknown tool: search (version 9.9)"


async def test_a_version_named_on_a_tool_that_has_none_is_refused():
    app = Veloce(title="Plain", openapi_url=None)

    @app.mcp_tool(name="search", description="Search")
    async def search(q: str) -> dict:
        return {"q": q}

    assert "error" in await _call(app, {"q": "x"}, version="1.0")


async def test_a_single_versioned_tool_answers_a_call_naming_its_version():
    app = Veloce(title="One", openapi_url=None)

    @app.mcp_tool(name="search", description="Search", version="3")
    async def search(q: str) -> dict:
        return {"q": q}

    assert _payload(await _call(app, {"q": "x"}, version="3"))["q"] == "x"


# ── The registrations that are still refused ─────────────────────────


def test_the_same_name_and_version_twice_is_still_a_duplicate():
    app = Veloce(title="Clash", openapi_url=None)

    @app.mcp_tool(name="search", description="One", version="1.0")
    async def first(q: str) -> dict:
        return {}

    @app.mcp_tool(name="search", description="Two", version="1.0")
    async def second(q: str) -> dict:
        return {}

    with pytest.raises(ValueError, match="Duplicate MCP tool name"):
        build_registry(app)


def test_a_versioned_and_an_unversioned_registration_still_collide():
    """Without a version on both there is no ordering, so there is no answer."""
    app = Veloce(title="Half", openapi_url=None)

    @app.mcp_tool(name="search", description="One")
    async def first(q: str) -> dict:
        return {}

    @app.mcp_tool(name="search", description="Two", version="2.0")
    async def second(q: str) -> dict:
        return {}

    with pytest.raises(ValueError, match="Duplicate MCP tool name"):
        build_registry(app)


def test_the_duplicate_message_offers_versioning_as_a_way_out():
    app = Veloce(title="Clash", openapi_url=None)

    @app.mcp_tool(name="search", description="One")
    async def first(q: str) -> dict:
        return {}

    @app.mcp_tool(name="search", description="Two")
    async def second(q: str) -> dict:
        return {}

    with pytest.raises(ValueError, match="version="):
        build_registry(app)


# ── An unversioned server is unchanged ───────────────────────────────


async def test_an_unversioned_tool_publishes_no_version_metadata():
    app = Veloce(title="Plain", openapi_url=None)

    @app.mcp_tool(description="Search")
    async def search(q: str) -> dict:
        return {"q": q}

    assert "_meta" not in (await _list(app))[0]


async def test_an_unversioned_server_keeps_an_empty_version_table():
    app = Veloce(title="Plain", openapi_url=None)

    @app.mcp_tool(description="Search")
    async def search(q: str) -> dict:
        return {"q": q}

    assert build_registry(app).versions == {}


# ── Alongside the author's own metadata ──────────────────────────────


async def test_declared_metadata_survives_alongside_the_version():
    app = Veloce(title="Meta", openapi_url=None)

    @app.mcp_tool(description="Search", version="1", meta={"io.example/team": "search"})
    async def search(q: str) -> dict:
        return {}

    published = (await _list(app))[0]["_meta"]
    assert published["io.example/team"] == "search"
    assert published["veloce"]["version"] == "1"


async def test_the_registered_version_wins_over_one_written_by_hand():
    app = Veloce(title="Meta", openapi_url=None)

    @app.mcp_tool(description="Search", version="2", meta={"veloce": {"version": "1", "note": "x"}})
    async def search(q: str) -> dict:
        return {}

    published = (await _list(app))[0]["_meta"]["veloce"]
    assert published["version"] == "2"
    assert published["note"] == "x"


# ── Non-numeric labels ───────────────────────────────────────────────


async def test_a_non_numeric_label_orders_after_every_numeric_one():
    """There is still an answer, whatever the author chose to write."""
    app = Veloce(title="Labelled", openapi_url=None)

    @app.mcp_tool(name="search", description="Numbered", version="2")
    async def numbered(q: str) -> dict:
        return {"which": "numbered"}

    @app.mcp_tool(name="search", description="Beta", version="beta")
    async def beta(q: str) -> dict:
        return {"which": "beta"}

    assert (await _list(app))[0]["description"] == "Beta"
    assert _payload(await _call(app, {"q": "x"}, version="2"))["which"] == "numbered"


# ── Each version keeps its own registration ──────────────────────────


async def test_an_older_version_keeps_its_own_scopes():
    """Reaching a version must not reach the listed one's authorization."""
    app = Veloce(title="Scoped", openapi_url=None)

    @app.mcp_tool(name="search", description="Old", version="1", scopes=["admin"])
    async def old(q: str) -> dict:
        return {"v": 1}

    @app.mcp_tool(name="search", description="New", version="2")
    async def new(q: str) -> dict:
        return {"v": 2}

    assert "error" in await _call(app, {"q": "x"}, version="1")
    assert _payload(await _call(app, {"q": "x"}))["v"] == 2
