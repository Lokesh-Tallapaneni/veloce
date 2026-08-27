"""Models, drivers and constants shared by the MCP test modules.

`test_mcp.py` defined these at module scope and used them across its own
sections; splitting the file by section meant they had to live somewhere all
the pieces could reach.

The models in particular cannot move into a test function. Under
`from __future__ import annotations` a handler's annotations are strings, and
`get_type_hints` resolves them against the handler's *global* namespace - a
class defined inside the test is not there.
"""

from __future__ import annotations

import asyncio

import orjson
from pydantic import BaseModel, Field, computed_field

from tests._mcp import Pipe
from veloce import (
    Principal,
    Veloce,
)
from veloce.contrib.mcp import MCPAuth
from veloce.contrib.mcp.server import MCPServer


def _server(app: Veloce) -> MCPServer:
    return MCPServer(app)


# Module-level so `get_type_hints` can resolve the annotation (a class defined
# inside a test function is not in the handler's global namespace).
class Item(BaseModel):
    name: str
    qty: int


class Address(BaseModel):
    city: str
    zip: str


class Customer(BaseModel):
    name: str
    address: Address


class PublicUser(BaseModel):
    id: int
    name: str


class FullUser(BaseModel):
    id: int
    name: str
    password: str


class AliasedOut(BaseModel):
    user_id: int = Field(alias="userId")
    name: str


class AnnotatedOut(BaseModel):
    id: int
    name: str


class Node(BaseModel):
    name: str
    children: list[Node] = []


Node.model_rebuild()


# A serialization-mode model: `b` is a computed field, absent from the
# validation schema but present in the serialization dump the client receives.
class ComputedOut(BaseModel):
    a: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def b(self) -> int:
        return self.a + 1


def _call(app: Veloce, name: str, arguments: dict) -> dict:
    """Drive one `tools/call` and return the single response object."""
    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    return asyncio.run(pipe.run())[0]


def _initialize(app: Veloce, params: dict) -> dict:
    """Drive one `initialize` and return the response object."""
    pipe = Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    return asyncio.run(pipe.run())[0]


def _list_tools(app: Veloce) -> dict[str, dict]:
    """Drive one `tools/list` and return the entries keyed by tool name."""
    pipe = Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    out = asyncio.run(pipe.run())[0]
    return {tool["name"]: tool for tool in out["result"]["tools"]}


def _list_resources(app: Veloce) -> dict[str, dict]:
    """Drive one `resources/list` and return the entries keyed by URI."""
    pipe = Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}})
    out = asyncio.run(pipe.run())[0]
    return {r["uri"]: r for r in out["result"]["resources"]}


def _list_resource_templates(app: Veloce) -> dict[str, dict]:
    """Drive one `resources/templates/list` and return the entries keyed by template."""
    pipe = Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list", "params": {}})
    out = asyncio.run(pipe.run())[0]
    return {r["uriTemplate"]: r for r in out["result"]["resourceTemplates"]}


def _read_resource(app: Veloce, uri: str) -> dict:
    """Drive one `resources/read` and return the single response object."""
    pipe = Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}})
    return asyncio.run(pipe.run())[0]


def _subscriptions_app() -> Veloce:
    """An app exposing one resource with resource subscriptions enabled."""
    app = Veloce(openapi_url=None)
    app.config["MCP_RESOURCE_SUBSCRIPTIONS"] = True

    @app.get(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
        mcp_description="Settings",
    )
    async def settings() -> dict:
        return {"theme": "dark"}

    return app


def _list_prompts(app: Veloce) -> dict[str, dict]:
    """Drive one `prompts/list` and return the entries keyed by prompt name."""
    pipe = Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "prompts/list", "params": {}})
    out = asyncio.run(pipe.run())[0]
    return {p["name"]: p for p in out["result"]["prompts"]}


def _get_prompt(app: Veloce, name: str, arguments: dict | None = None) -> dict:
    """Drive one `prompts/get` and return the single response object."""
    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "prompts/get",
            "params": {"name": name, "arguments": arguments or {}},
        }
    )
    return asyncio.run(pipe.run())[0]


def _drive_call(app: Veloce, name: str, arguments: dict | None = None):
    """Drive one `tools/call` through the transport and return every written line."""
    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
    )
    return pipe.run()


def _parse_sse(body: bytes) -> list[dict]:
    """Extract the JSON payloads from the `data:` lines of an SSE body.

    The stream's priming frame carries an empty `data:` field (no JSON), so an
    empty payload is skipped rather than decoded.
    """
    events: list[dict] = []
    for raw in body.split(b"\n"):
        line = raw.strip()
        if line.startswith(b"data:"):
            payload = line[len(b"data:") :].strip()
            if payload:
                events.append(orjson.loads(payload))
    return events


async def _drive_stream(stream: object) -> None:
    """Run an SSE response generator to exhaustion (its dispatch task settles)."""
    async for _ in stream._stream:  # type: ignore[attr-defined]
        pass


def _verify(token: str):
    """A toy verifier: 'good' -> a scoped principal, anything else -> reject."""
    if token == "good":
        return Principal(subject="agent-1", scopes=frozenset({"mcp:tools"}))
    if token == "noscope":
        return Principal(subject="agent-2", scopes=frozenset())
    return None


def _auth(**kw) -> MCPAuth:
    """MCPAuth with the spec-required metadata filled in (verify defaults to _verify)."""
    kw.setdefault("verify", _verify)
    kw.setdefault("resource_server_url", "https://api.example.com/mcp")
    kw.setdefault("authorization_servers", ["https://auth.example.com"])
    return MCPAuth(**kw)


def _mcp_call_body(name: str, arguments: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def _sse_event_ids(body: bytes) -> list[str]:
    """Return the `id:` field values from an SSE body, in order."""
    ids: list[str] = []
    for raw in body.split(b"\n"):
        line = raw.strip()
        if line.startswith(b"id:"):
            ids.append(line[len(b"id:") :].strip().decode("utf-8"))
    return ids
