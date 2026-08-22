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
from veloce.contrib.mcp.transform import derive_tool
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


# ── A copy must not advertise what it cannot serve ───────────────────


def _versioned_app() -> Veloce:
    app = Veloce(title="Versioned", version="1.0.0", openapi_url=None)

    @app.mcp_tool(name="calc", description="Add two numbers", version="1.0")
    async def calc_v1(a: int, b: int) -> int:
        return a + b

    @app.mcp_tool(name="calc", description="Add two numbers", version="2.0")
    async def calc_v2(a: int, b: int, precision: int = 0) -> int:
        return a + b

    return app


def _published_versions(app: Veloce, name: str) -> tuple[str | None, list[str]]:
    entry = MCPServer(app)._describe_tool(build_registry(app).tools[name])
    published = (entry.get("_meta") or {}).get("veloce") or {}
    return published.get("version"), published.get("versions", [])


def _every_published_version_resolves(app: Veloce) -> bool:
    """The invariant: what a tool advertises is what the server can dispatch."""
    registry = build_registry(app)
    server = MCPServer(app)
    for name, tool in registry.tools.items():
        entry = server._describe_tool(tool)
        for version in ((entry.get("_meta") or {}).get("veloce") or {}).get("versions", []):
            if registry.resolve(name, version) is None:
                return False
    return True


def test_the_original_still_advertises_every_version_it_serves():
    assert _published_versions(_versioned_app(), "calc") == ("2.0", ["1.0", "2.0"])


def test_a_derived_tool_does_not_inherit_the_version_set():
    """It wraps one version's surface, so it can answer for one version."""
    app = _versioned_app()
    app.add_mcp_tool(derive_tool(build_registry(app).tools["calc"], name="calculate"))
    assert _published_versions(app, "calculate") == ("2.0", [])


async def test_a_derived_tool_serves_its_own_version():
    app = _versioned_app()
    app.add_mcp_tool(derive_tool(build_registry(app).tools["calc"], name="calculate"))
    params: dict = {
        "name": "calculate",
        "arguments": {"a": 4, "b": 6},
        "_meta": {"veloce": {"version": "2.0"}},
    }
    response = await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}, MCPSession()
    )
    assert response["result"]["content"][0]["text"] == "10"


async def test_a_derived_tool_refuses_a_version_it_never_had():
    app = _versioned_app()
    app.add_mcp_tool(derive_tool(build_registry(app).tools["calc"], name="calculate"))
    assert "error" in await _call(app, {"a": 1, "b": 1}, version="1.0")


def test_a_namespaced_mount_does_not_inherit_the_version_set():
    parent = Veloce(title="Parent", openapi_url=None)
    parent.mount("/billing", _versioned_app(), expose_mcp=True)
    assert _published_versions(parent, "billing_calc") == ("2.0", [])


def test_what_a_tool_advertises_is_what_the_server_can_dispatch():
    """The invariant, over every shape that copies a tool."""
    parent = Veloce(title="Parent", openapi_url=None)
    parent.mount("/billing", _versioned_app(), expose_mcp=True)
    app = _versioned_app()
    app.add_mcp_tool(derive_tool(build_registry(app).tools["calc"], name="calculate"))
    assert _every_published_version_resolves(parent)
    assert _every_published_version_resolves(app)


async def test_a_proxied_tool_publishes_no_version_the_gateway_cannot_serve():
    """The upstream can answer `calc@1.0`; a gateway forwarding by name cannot."""
    from veloce.contrib.mcp.proxy import add_mcp_proxy

    upstream = MCPServer(_versioned_app())

    async def request(method: str, params: dict) -> dict:
        response = await upstream.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, MCPSession()
        )
        return response["result"]

    gateway = Veloce(title="Gateway", openapi_url=None)
    await add_mcp_proxy(gateway, "up", request)
    assert _published_versions(gateway, "up_calc") == (None, [])
    assert _every_published_version_resolves(gateway)


async def test_other_upstream_metadata_still_travels():
    """Only the block this server cannot honour is dropped."""
    from veloce.contrib.mcp.proxy import add_mcp_proxy

    async def request(method: str, params: dict) -> dict:
        return {
            "tools": [
                {
                    "name": "add",
                    "inputSchema": {},
                    "_meta": {"io.example/team": "math", "veloce": {"version": "9"}},
                }
            ]
        }

    gateway = Veloce(title="Gateway", openapi_url=None)
    await add_mcp_proxy(gateway, "up", request)
    assert build_registry(gateway).tools["up_add"].meta == {"io.example/team": "math"}
