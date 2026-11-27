"""A parameter declared on a sub-dependency reaches the OpenAPI document.

`iter_param_descriptors` - described in its own docstring as "the single
canonical walk" - did not recurse `slot.sub_plan`. The runtime resolver does, so
the two doors disagreed about what the route accepts:

| door | result |
|---|---|
| runtime | `GET /v` -> **422**, missing required parameter `q` |
| MCP | `{'properties': {'q': {'type':'integer'}}, 'required': ['q']}` |
| OpenAPI | `parameters: None` |

A client generated from that schema cannot call the route: it does not know `q`
exists, and the 422 it gets back names a parameter the contract never mentioned.
MCP was right because it had written its *own* recursive walk rather than using
the canonical one, which is the whole reason the canonical one's gap went unseen.

The walk now recurses, with the two rules MCP's copy already had and needs:

* **A cycle guard.** The plan builder forbids a `Depends` cycle, but a diamond -
  two dependencies sharing a third - reaches the same sub-plan twice.
* **First declaration of a name wins.** A dependency cached and injected twice is
  one wire parameter, not two.
"""

from __future__ import annotations

from typing import Annotated

import pytest

from veloce import Depends, Header, Query, Veloce
from veloce.testclient import TestClient


async def paging(q: int = Query()) -> int:
    return q


async def optional_page(page: int = Query(default=1, ge=1)) -> int:
    return page


async def api_key(key: str = Header(alias="x-api-key")) -> str:
    return key


async def nested(inner: int = Depends(paging)) -> int:
    return inner


def _params(build) -> list[dict]:
    app = Veloce(title="T", version="1")
    build(app)
    operation = app.openapi()["paths"]["/v"]["get"]
    return operation.get("parameters") or []


def _names(build) -> set[str]:
    return {p["name"] for p in _params(build)}


# ── the parameter is published ───────────────────────────────────────


def test_a_sub_dependency_query_parameter_is_published():
    """The defect: `parameters` was `None`, so `q` was undiscoverable."""

    def build(app):
        @app.get("/v")
        async def v(page: int = Depends(paging)) -> dict:
            return {}

    assert "q" in _names(build)


def test_the_published_parameter_carries_its_type():
    def build(app):
        @app.get("/v")
        async def v(page: int = Depends(paging)) -> dict:
            return {}

    q = next(p for p in _params(build) if p["name"] == "q")
    assert q["schema"]["type"] == "integer"
    assert q["in"] == "query"


def test_a_required_sub_dependency_parameter_is_marked_required():
    """The runtime 422s without it, so the contract must say so."""

    def build(app):
        @app.get("/v")
        async def v(page: int = Depends(paging)) -> dict:
            return {}

    assert next(p for p in _params(build) if p["name"] == "q")["required"] is True


def test_an_optional_sub_dependency_parameter_is_not_required():
    def build(app):
        @app.get("/v")
        async def v(page: int = Depends(optional_page)) -> dict:
            return {}

    assert next(p for p in _params(build) if p["name"] == "page")["required"] is False


def test_a_sub_dependency_header_is_published_in_the_right_place():
    def build(app):
        @app.get("/v")
        async def v(key: str = Depends(api_key)) -> dict:
            return {}

    key = next(p for p in _params(build) if p["name"] == "x-api-key")
    assert key["in"] == "header"


def test_a_parameter_two_levels_down_is_published():
    """The walk has to be recursive, not one level deep."""

    def build(app):
        @app.get("/v")
        async def v(page: int = Depends(nested)) -> dict:
            return {}

    assert "q" in _names(build)


def test_an_annotated_sub_dependency_is_published():
    """The `Annotated` spelling reaches the same walk."""

    def build(app):
        @app.get("/v")
        async def v(page: Annotated[int, Depends(paging)]) -> dict:
            return {}

    assert "q" in _names(build)


def test_top_level_and_sub_dependency_parameters_appear_together():
    def build(app):
        @app.get("/v")
        async def v(limit: int = 10, page: int = Depends(paging)) -> dict:
            return {}

    assert {"limit", "q"} <= _names(build)


# ── the two rules the recursion needs ────────────────────────────────


def test_one_dependency_injected_twice_publishes_one_parameter():
    """A cached dependency is one wire parameter, not two."""

    def build(app):
        @app.get("/v")
        async def v(a: int = Depends(paging), b: int = Depends(paging)) -> dict:
            return {}

    names = [p["name"] for p in _params(build)]
    assert names.count("q") == 1


def test_a_diamond_dependency_graph_terminates():
    """Two dependencies sharing a third reach the same sub-plan twice."""

    async def left(v: int = Depends(paging)) -> int:
        return v

    async def right(v: int = Depends(paging)) -> int:
        return v

    def build(app):
        @app.get("/v")
        async def v(a: int = Depends(left), b: int = Depends(right)) -> dict:
            return {}

    names = [p["name"] for p in _params(build)]
    assert names.count("q") == 1


def test_a_top_level_parameter_wins_over_a_sub_dependency_of_the_same_name():
    """First declaration wins, and the top level is walked first."""

    def build(app):
        @app.get("/v")
        async def v(q: str = "top", page: int = Depends(paging)) -> dict:
            return {}

    matching = [p for p in _params(build) if p["name"] == "q"]
    assert len(matching) == 1
    assert matching[0]["schema"]["type"] == "string"


# ── nothing that is not an input gets published ──────────────────────


def test_an_injected_request_is_not_published():
    """The negative: inject-only slots must stay out of the contract."""
    from veloce import Request

    async def with_request(request: Request) -> str:
        return request.path

    def build(app):
        @app.get("/v")
        async def v(path: str = Depends(with_request)) -> dict:
            return {}

    assert _names(build) == set()


def test_a_security_scheme_dependency_is_not_published_as_a_parameter():
    """A scheme is published under `security`, not as a query parameter."""
    from veloce import HTTPBearer, Security

    bearer = HTTPBearer()

    def build(app):
        @app.get("/v")
        async def v(cred: str = Security(bearer)) -> dict:
            return {}

    assert "Authorization" not in _names(build)


def test_a_route_with_no_dependencies_is_unchanged():
    def build(app):
        @app.get("/v")
        async def v(limit: int = 10) -> dict:
            return {}

    assert _names(build) == {"limit"}


def test_a_route_with_no_parameters_publishes_none():
    def build(app):
        @app.get("/v")
        async def v() -> dict:
            return {}

    assert _params(build) == []


# ── the contract matches what the runtime actually enforces ──────────


def test_the_documented_parameter_is_the_one_the_runtime_requires():
    """The property the whole finding is about: one route, one answer."""
    app = Veloce(title="T", version="1")

    @app.get("/v")
    async def v(page: int = Depends(paging)) -> dict:
        return {"page": page}

    client = TestClient(app)
    assert client.get("/v").status_code == 422
    assert client.get("/v", params={"q": "5"}).json() == {"page": 5}

    published = {p["name"] for p in app.openapi()["paths"]["/v"]["get"]["parameters"]}
    assert "q" in published


def test_the_mcp_and_openapi_doors_agree():
    """MCP had its own recursive walk and was right; they now share one."""
    pytest.importorskip("veloce.contrib.mcp")
    app = Veloce(title="T", version="1")

    @app.get("/v", expose_as_mcp_tool=True, mcp_description="v")
    async def v(page: int = Depends(paging)) -> dict:
        return {}

    app.mount_mcp(transport="http", path="/mcp")
    client = TestClient(app)
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "p", "version": "1"},
            },
        },
        headers={"Accept": "application/json"},
    )
    listing = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json"},
    ).json()
    tool = listing["result"]["tools"][0]
    mcp_inputs = set(tool["inputSchema"].get("properties", {}))
    openapi_inputs = {p["name"] for p in app.openapi()["paths"]["/v"]["get"]["parameters"]}
    assert mcp_inputs == openapi_inputs == {"q"}
