"""MCP integration tests - registry, schema, stdio round-trip, DI, safety."""

from __future__ import annotations

import asyncio
import base64
import time

import orjson
import pytest
from pydantic import BaseModel, Field, computed_field

from veloce import (
    BackgroundTasks,
    Blueprint,
    Depends,
    HTTPException,
    JSONResponse,
    MCPContext,
    Principal,
    Response,
    SecurityScopes,
    Veloce,
    current_principal,
    set_principal,
)
from veloce.contrib.mcp import MCPAuth
from veloce.contrib.mcp.registry import build_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.transports.stdio import StdioTransport

# -- In-process stdio driver ------------------------------------------


class _Pipe:
    """Drive a `StdioTransport` in-process: feed request lines, collect replies."""

    def __init__(self, server: MCPServer) -> None:
        self._inbox: list[bytes] = []
        self.outbox: list[dict] = []
        self.transport = StdioTransport(server, self._read_line, self._write_line)

    def feed(self, message: dict) -> None:
        self._inbox.append(orjson.dumps(message))

    async def _read_line(self) -> bytes | None:
        if not self._inbox:
            return None
        return self._inbox.pop(0)

    async def _write_line(self, data: bytes) -> None:
        self.outbox.append(orjson.loads(data))

    async def run(self) -> list[dict]:
        await self.transport.serve()
        return self.outbox


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


# -- Registration -----------------------------------------------------


def test_mcp_tool_registration_explicit():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    registry = build_registry(app)
    assert "add" in registry.tools
    tool = registry.tools["add"]
    assert tool.description == "Add two integers"


def test_expose_existing_get_route():
    app = Veloce(openapi_url=None)

    @app.get("/ping", expose_as_mcp_tool=True, mcp_description="Health probe")
    async def ping():
        return {"pong": True}

    registry = build_registry(app)
    assert "ping" in registry.tools
    assert registry.tools["ping"].description == "Health probe"


def test_shared_handler_two_routes_become_two_tools():
    """One handler mounted as two distinct named routes yields two tools.

    Deduplication is by route, not by the handler callable, so a function
    intentionally mounted twice is not silently collapsed into a single tool.
    """
    app = Veloce(openapi_url=None)

    async def health():
        return {"ok": True}

    app.add_route(
        "/ping",
        health,
        methods=["GET"],
        name="ping",
        expose_as_mcp_tool=True,
        mcp_description="Ping",
    )
    app.add_route(
        "/healthz",
        health,
        methods=["GET"],
        name="healthz",
        expose_as_mcp_tool=True,
        mcp_description="Healthz",
    )

    registry = build_registry(app)
    assert "ping" in registry.tools
    assert "healthz" in registry.tools
    # Both tools wrap the same callable but are independently callable.
    assert registry.tools["ping"].handler is registry.tools["healthz"].handler


# -- Schema generation ------------------------------------------------


def test_input_schema_from_signature():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Echo with options")
    async def echo(text: str, times: int = 1, loud: bool = False) -> str:
        return (text.upper() if loud else text) * times

    registry = build_registry(app)
    schema = registry.tools["echo"].input_schema
    assert schema["type"] == "object"
    props = schema["properties"]
    assert props["text"] == {"type": "string"}
    assert props["times"] == {"type": "integer"}
    assert props["loud"] == {"type": "boolean"}
    # `text` is required (no default); `times` / `loud` are not.
    assert schema["required"] == ["text"]


def test_input_schema_with_pydantic_model():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Create an item")
    async def create(item: Item) -> dict:
        return item.model_dump()

    registry = build_registry(app)
    schema = registry.tools["create"].input_schema
    assert "item" in schema["properties"]
    # The ref is local to this standalone schema, and the model's fields are
    # resolvable from `$defs` without any external component envelope.
    ref = schema["properties"]["item"]["$ref"]
    assert ref == "#/$defs/Item"
    assert "Item" in schema["$defs"]
    item_props = schema["$defs"]["Item"]["properties"]
    assert set(item_props) == {"name", "qty"}


def test_pydantic_input_schema_is_self_contained():
    """tools/list inputSchema must resolve model fields with no external lookup."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Create an item")
    async def create(item: Item) -> dict:
        return item.model_dump()

    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    out = asyncio.run(pipe.run())

    tool = next(t for t in out[0]["result"]["tools"] if t["name"] == "create")
    schema = tool["inputSchema"]
    ref = schema["properties"]["item"]["$ref"]
    # No dangling OpenAPI component ref: the ref points into the schema's own
    # `$defs`, and that def is present in the same tools/list payload.
    assert ref.startswith("#/$defs/")
    name = ref.split("/")[-1]
    assert name in schema["$defs"]
    assert "components" not in schema
    assert set(schema["$defs"][name]["properties"]) == {"name", "qty"}


def test_nested_pydantic_input_schema_is_self_contained():
    """A model that embeds another model must inline both into `$defs`.

    Pydantic keeps the inner model's ref in its native `#/$defs/<Name>` form
    inside the outer component; the bridge must still pull that component out
    of the shared registry so the tool schema has no dangling ref.
    """
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Create a customer")
    async def create_customer(customer: Customer) -> dict:
        return customer.model_dump()

    registry = build_registry(app)
    schema = registry.tools["create_customer"].input_schema
    defs = schema["$defs"]
    # Both the outer and the nested model are present, with no dangling ref.
    assert "Customer" in defs
    assert "Address" in defs
    inner_ref = defs["Customer"]["properties"]["address"]["$ref"]
    assert inner_ref == "#/$defs/Address"
    assert set(defs["Address"]["properties"]) == {"city", "zip"}


def test_context_param_excluded_from_schema():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Tool with context")
    async def with_ctx(value: int, ctx: MCPContext) -> int:
        return value

    registry = build_registry(app)
    schema = registry.tools["with_ctx"].input_schema
    assert "value" in schema["properties"]
    assert "ctx" not in schema["properties"]


# -- tools/list + tools/call round-trip -------------------------------


def test_tools_list_and_call_round_trip():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    pipe.feed({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 5}},
        }
    )
    out = asyncio.run(pipe.run())

    init, listed, called = out
    assert init["result"]["serverInfo"]["name"]
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "add" in names
    assert called["result"]["content"][0]["text"] == "7"


def test_call_coerces_string_arguments():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Double an integer")
    async def double(n: int) -> int:
        return n * 2

    pipe = _Pipe(_server(app))
    # JSON argument arrives as a string; the bridge coerces it to int.
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "double", "arguments": {"n": "21"}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "42"


def test_call_pydantic_body_model():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Summarise an item")
    async def summarise(item: Item) -> str:
        return f"{item.qty}x {item.name}"

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "summarise", "arguments": {"item": {"name": "widget", "qty": 3}}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "3x widget"


def test_missing_required_argument_is_invalid_params():
    """A missing required argument is an invalid-params transport error."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 1}},
        }
    )
    out = asyncio.run(pipe.run())
    # Argument-binding failure routes to the JSON-RPC error channel, not an
    # in-band result, so the agent learns its call was malformed.
    assert "result" not in out[0]
    assert out[0]["error"]["code"] == -32602


def test_handler_internal_type_error_is_in_band():
    """A TypeError raised inside the handler body is an in-band tool error."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Trip on a bad operand inside the body")
    async def buggy(n: int) -> int:
        # A genuine handler bug raises TypeError; it must not be misread as an
        # invalid-params transport error.
        return n + "x"  # type: ignore[operator]

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "buggy", "arguments": {"n": 1}},
        }
    )
    out = asyncio.run(pipe.run())
    # The handler error is surfaced in-band (isError=true), never on the
    # JSON-RPC error channel.
    assert "error" not in out[0]
    assert out[0]["result"]["isError"] is True


def test_unknown_method_returns_method_not_found():
    app = Veloce(openapi_url=None)
    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 9, "method": "does/not/exist", "params": {}})
    out = asyncio.run(pipe.run())
    assert out[0]["error"]["code"] == -32601


def test_notification_yields_no_response():
    app = Veloce(openapi_url=None)
    pipe = _Pipe(_server(app))
    # No `id` -> notification, no response written.
    pipe.feed({"jsonrpc": "2.0", "method": "notifications/initialized"})
    out = asyncio.run(pipe.run())
    assert out == []


def test_parse_error_on_bad_json():
    app = Veloce(openapi_url=None)
    server = _server(app)
    transport = StdioTransport(server, None, None)  # type: ignore[arg-type]
    out = asyncio.run(transport._dispatch_line(b"{not json"))
    assert out["error"]["code"] == -32700


def test_stdio_transport_satisfies_transport_contract():
    from veloce.contrib.mcp.transports.base import Transport

    app = Veloce(openapi_url=None)
    transport = StdioTransport(_server(app), None, None)  # type: ignore[arg-type]
    assert isinstance(transport, Transport)


def test_stdio_transport_send_writes_one_message():
    app = Veloce(openapi_url=None)
    written: list[bytes] = []

    async def write_line(data: bytes) -> None:
        written.append(data)

    transport = StdioTransport(_server(app), None, write_line)  # type: ignore[arg-type]
    asyncio.run(transport.send({"jsonrpc": "2.0", "method": "notifications/progress"}))
    assert orjson.loads(written[0])["method"] == "notifications/progress"


# -- Dependency injection ---------------------------------------------


def test_dependency_injection_on_tool_call():
    app = Veloce(openapi_url=None)

    def get_multiplier() -> int:
        return 10

    @app.mcp_tool(description="Multiply by an injected factor")
    async def scaled(n: int, factor: int = Depends(get_multiplier)) -> int:
        return n * factor

    # `factor` is injected, so it must not appear as an agent input.
    registry = build_registry(app)
    assert "factor" not in registry.tools["scaled"].input_schema["properties"]

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "scaled", "arguments": {"n": 4}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "40"


def test_route_level_dependency_runs_on_tool_call():
    app = Veloce(openapi_url=None)
    events: list[str] = []

    def audit() -> None:
        events.append("guard")

    @app.get(
        "/report",
        expose_as_mcp_tool=True,
        mcp_description="Fetch the report",
        dependencies=[Depends(audit)],
    )
    async def report() -> dict:
        events.append("handler")
        return {"ok": True}

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "report", "arguments": {}},
        }
    )
    asyncio.run(pipe.run())
    # The route-level dependency runs before the handler, exactly as on the
    # HTTP/WS paths.
    assert events == ["guard", "handler"]


def test_route_level_dependency_can_reject_tool_call():
    app = Veloce(openapi_url=None)
    reached: list[str] = []

    def deny() -> None:
        raise HTTPException(status_code=403, detail="forbidden")

    @app.get(
        "/secret",
        expose_as_mcp_tool=True,
        mcp_description="Read the secret",
        dependencies=[Depends(deny)],
    )
    async def secret() -> dict:
        reached.append("handler")
        return {"secret": 42}

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "secret", "arguments": {}},
        }
    )
    out = asyncio.run(pipe.run())
    # A rejecting route guard aborts the call: the handler never runs and the
    # result is an in-band tool error.
    assert reached == []
    assert out[0]["result"]["isError"] is True


def test_yield_dependency_teardown_runs():
    app = Veloce(openapi_url=None)
    events: list[str] = []

    def resource():
        events.append("setup")
        yield "db"
        events.append("teardown")

    @app.mcp_tool(description="Use a yield dependency")
    async def use_res(res: str = Depends(resource)) -> str:
        events.append(f"use:{res}")
        return res

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "use_res", "arguments": {}},
        }
    )
    asyncio.run(pipe.run())
    assert events == ["setup", "use:db", "teardown"]


def test_context_is_injected():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Return the calling tool name")
    async def whoami(ctx: MCPContext) -> str:
        return ctx.tool_name

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "whoami"


def test_dependency_consumes_tool_argument():
    """A `Depends()` sub-dependency resolves a parameter named like a tool
    argument from the agent-supplied arguments, mirroring how the HTTP path
    feeds path/query params into sub-dependencies."""
    app = Veloce(openapi_url=None)

    def get_user_id(user_id: int) -> int:
        # The sub-dependency declares the same name the agent supplies; it must
        # receive the coerced argument value, not fall over looking for an HTTP
        # request attribute.
        return user_id

    @app.mcp_tool(description="Echo the resolved user id")
    async def lookup(resolved: int = Depends(get_user_id)) -> int:
        return resolved

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "lookup", "arguments": {"user_id": "42"}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "42"


def test_malformed_argument_type_is_invalid_params():
    """A value that fails coercion onto the parameter type is an invalid-params
    transport error, not an in-band handler failure."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Double an integer")
    async def double(n: int) -> int:
        return n * 2

    pipe = _Pipe(_server(app))
    # `"abc"` cannot coerce to int; this is a malformed call, so it routes to
    # the JSON-RPC error channel rather than isError=true.
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "double", "arguments": {"n": "abc"}},
        }
    )
    out = asyncio.run(pipe.run())
    assert "result" not in out[0]
    assert out[0]["error"]["code"] == -32602


def test_malformed_model_argument_is_invalid_params():
    """A body model that fails validation is an invalid-params transport error."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Summarise an item")
    async def summarise(item: Item) -> str:
        return f"{item.qty}x {item.name}"

    pipe = _Pipe(_server(app))
    # `qty` is not an integer; model validation fails at the binding boundary.
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "summarise", "arguments": {"item": {"name": "x", "qty": "no"}}},
        }
    )
    out = asyncio.run(pipe.run())
    assert "result" not in out[0]
    assert out[0]["error"]["code"] == -32602


def test_security_scopes_param_injected():
    """A handler declaring `scopes: SecurityScopes` is callable over MCP and
    receives an empty SecurityScopes (no enclosing Security() chain)."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Report the security scopes")
    async def whatscopes(scopes: SecurityScopes) -> str:
        # An MCP call has no Security() chain, so the scope list is empty.
        assert isinstance(scopes, SecurityScopes)
        return ",".join(scopes.scopes)

    # The SecurityScopes slot is not an agent input.
    registry = build_registry(app)
    assert "scopes" not in registry.tools["whatscopes"].input_schema["properties"]

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "whatscopes", "arguments": {}},
        }
    )
    out = asyncio.run(pipe.run())
    assert "error" not in out[0]
    assert out[0]["result"]["content"][0]["text"] == ""


def test_sync_handler_offloaded():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Sync add")
    def sync_add(a: int, b: int) -> int:
        return a + b

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "sync_add", "arguments": {"a": 3, "b": 4}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "7"


# -- Safety rules -----------------------------------------------------


def test_post_route_not_auto_exposed():
    app = Veloce(openapi_url=None)

    # A POST route with no explicit opt-in must NOT become a tool.
    @app.post("/items")
    async def create_item():
        return {"ok": True}

    registry = build_registry(app)
    assert registry.tools == {}


def test_post_route_explicit_opt_in_exposed():
    app = Veloce(openapi_url=None)

    @app.post("/items", expose_as_mcp_tool=True, mcp_description="Create an item")
    async def create_item():
        return {"ok": True}

    registry = build_registry(app)
    assert "create_item" in registry.tools


def test_missing_mcp_description_raises_on_exposed_route():
    app = Veloce(openapi_url=None)

    @app.get("/x", expose_as_mcp_tool=True)
    async def x():
        return {}

    with pytest.raises(ValueError, match="description"):
        build_registry(app)


def test_missing_description_raises_on_mcp_tool_decorator():
    app = Veloce(openapi_url=None)

    with pytest.raises(ValueError, match="description"):

        @app.mcp_tool(description="")
        async def bad():
            return None


# -- Blueprint namespacing --------------------------------------------


def test_blueprint_namespacing():
    app = Veloce(openapi_url=None)
    bp = Blueprint("billing", url_prefix="/billing")

    @bp.get("/status", expose_as_mcp_tool=True, mcp_description="Billing status")
    async def status():
        return {"ok": True}

    app.register_blueprint(bp)

    registry = build_registry(app)
    # Blueprint route name is `billing.status` -> tool name `billing_status`.
    assert "billing_status" in registry.tools


def test_mcp_tool_namespace_prefix():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Namespaced add", namespace="math")
    async def add(a: int, b: int) -> int:
        return a + b

    registry = build_registry(app)
    assert "math_add" in registry.tools


# -- Instrumentation --------------------------------------------------


def test_instrumentation_fires_on_tool_call():
    app = Veloce(openapi_url=None)
    seen: list[tuple[str, str | None]] = []

    @app.add_instrumentation
    def record(metrics):
        seen.append((metrics.method, metrics.route))

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 1, "b": 2}},
        }
    )
    asyncio.run(pipe.run())
    assert ("tools/call", "add") in seen


def test_mount_mcp_rejects_unknown_transport():
    app = Veloce(openapi_url=None)
    with pytest.raises(ValueError, match="transport"):
        app.mount_mcp(transport="carrier-pigeon")


def test_duplicate_tool_name_raises():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="One")
    async def dup():
        return 1

    # A second tool resolving to the same name collides at registry build.
    app._mcp_tools.append((dup, "dup", "Two", None, None))
    with pytest.raises(ValueError, match="Duplicate"):
        build_registry(app)


# -- Response shaping for exposed routes ------------------------------


def _call(app: Veloce, name: str, arguments: dict) -> dict:
    """Drive one `tools/call` and return the single response object."""
    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    return asyncio.run(pipe.run())[0]


def test_exposed_route_response_model_filters_excluded_fields():
    """An exposed route's `response_model` filters the handler return over MCP,
    so a field absent from the response model never leaks to the agent."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/me",
        expose_as_mcp_tool=True,
        mcp_description="Current user",
        response_model=PublicUser,
    )
    async def me() -> dict:
        # The handler returns a password, but `response_model=PublicUser` has no
        # such field, so it must be dropped before the value reaches the agent.
        return {"id": 1, "name": "ada", "password": "s3cret"}

    out = _call(app, "me", {})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"id": 1, "name": "ada"}
    assert "password" not in payload


def test_exposed_route_response_model_exclude_filters_field():
    """`response_model_exclude` hides a declared field over MCP as it does on HTTP."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/full",
        expose_as_mcp_tool=True,
        mcp_description="Full user",
        response_model=FullUser,
        response_model_exclude={"password"},
    )
    async def full() -> FullUser:
        return FullUser(id=2, name="grace", password="hunter2")

    out = _call(app, "full", {})
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"id": 2, "name": "grace"}


def test_exposed_route_returning_jsonresponse_yields_decoded_body():
    """A handler returning a JSONResponse yields its decoded body, not a repr."""
    app = Veloce(openapi_url=None)

    @app.get("/data", expose_as_mcp_tool=True, mcp_description="Raw data")
    async def data() -> JSONResponse:
        return JSONResponse({"value": 42, "items": [1, 2, 3]})

    out = _call(app, "data", {})
    text = out["result"]["content"][0]["text"]
    assert "JSONResponse" not in text
    assert orjson.loads(text) == {"value": 42, "items": [1, 2, 3]}


def test_exposed_route_returning_plain_response_yields_body_text():
    """A handler returning a plain Response yields the body text, not a repr."""
    app = Veloce(openapi_url=None)

    @app.get("/txt", expose_as_mcp_tool=True, mcp_description="Plain text")
    async def txt() -> Response:
        return Response(body=b"hello world", content_type="text/plain")

    out = _call(app, "txt", {})
    assert out["result"]["content"][0]["text"] == "hello world"


def test_exposed_route_text_json_body_is_not_json_decoded():
    """`text/json` is not `application/json`: a JSON-looking body under that
    content type is returned as verbatim text, never decoded and re-serialised.
    Guards the `is_json_mimetype` over-match fix (`endswith("json")` matched
    `text/json`)."""
    app = Veloce(openapi_url=None)

    @app.get("/tj", expose_as_mcp_tool=True, mcp_description="text/json body")
    async def tj() -> Response:
        return Response(body=b'{"x": 1}', content_type="text/json")

    out = _call(app, "tj", {})
    assert out["result"]["content"][0]["text"] == '{"x": 1}'


# -- Request injection ------------------------------------------------


def test_request_slot_receives_real_request():
    """A handler declaring `request: Request` receives a real, empty Request:
    `request.headers.get(...)` returns nothing and `request.state` is usable."""
    app = Veloce(openapi_url=None)

    from veloce import Request

    @app.mcp_tool(description="Read request state and a header")
    async def probe(request: Request) -> dict:
        request.state.touched = True
        return {
            "auth": request.headers.get("authorization"),
            "touched": request.state.touched,
        }

    out = _call(app, "probe", {})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"auth": None, "touched": True}


def test_dependency_reading_request_state_works():
    """A dependency that reads/writes `request.state` works over MCP (it gets a
    real Request, not a bare MCPContext) - no AttributeError."""
    app = Veloce(openapi_url=None)

    from veloce import Request

    def stamp(request: Request) -> int:
        # Would raise AttributeError if a bare MCPContext were substituted here.
        request.state.x = 7
        return request.state.x

    @app.mcp_tool(description="Use a request-reading dependency")
    async def use_stamp(value: int = Depends(stamp)) -> int:
        return value

    out = _call(app, "use_stamp", {})
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "7"


# -- Response / BackgroundTasks injection -----------------------------


def test_response_slot_injected():
    """A handler declaring `response: Response` is callable; it gets a fresh
    Response to mutate without a missing-argument TypeError."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Mutate the injected response")
    async def with_response(response: Response) -> str:
        response.status_code = 201
        return "ok"

    # The Response slot is framework-injected, never an agent input.
    registry = build_registry(app)
    assert "response" not in registry.tools["with_response"].input_schema["properties"]

    out = _call(app, "with_response", {})
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "ok"


def test_background_tasks_slot_injected_and_runs():
    """A handler declaring `tasks: BackgroundTasks` is callable and the
    scheduled task actually runs after the handler returns."""
    app = Veloce(openapi_url=None)
    ran: list[str] = []

    async def record() -> None:
        ran.append("done")

    @app.mcp_tool(description="Schedule a background task")
    async def schedule(tasks: BackgroundTasks) -> str:
        tasks.add_task(record)
        return "queued"

    # The BackgroundTasks slot is framework-injected, never an agent input.
    registry = build_registry(app)
    assert "tasks" not in registry.tools["schedule"].input_schema["properties"]

    out = _call(app, "schedule", {})
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "queued"
    # The background task ran after the handler returned, mirroring HTTP.
    assert ran == ["done"]


# -- Type-based context detection -------------------------------------


def test_plain_argument_named_context_is_an_input():
    """A tool argument named `context` but typed `str` stays a normal input -
    detection of the MCPContext is by TYPE, never by parameter name."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Echo a string named context")
    async def echo(context: str) -> str:
        return context

    # `context` is a real, required agent input - it appears in the schema.
    registry = build_registry(app)
    props = registry.tools["echo"].input_schema["properties"]
    assert "context" in props

    out = _call(app, "echo", {"context": "hi"})
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "hi"


# -- Lifespan, request context, streaming, response background --------


def test_mount_mcp_enters_lifespan_before_serving():
    """The serve loop runs inside `lifespan_context()`, so an `on_startup` hook
    that populates `app.state` has run before the first tool call reads it."""
    app = Veloce(openapi_url=None)

    @app.on_startup
    async def _seed() -> None:
        app.state.greeting = "ready"

    @app.mcp_tool(description="Read a value seeded at startup")
    async def read_seed() -> str:
        # Fails (AttributeError) if startup never ran before the call.
        return app.state.greeting

    async def _drive() -> dict:
        # Mirror `mount_mcp`: drive the server inside the app lifespan in-process.
        async with app.lifespan_context():
            pipe = _Pipe(_server(app))
            pipe.feed(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "read_seed", "arguments": {}},
                }
            )
            return (await pipe.run())[0]

    out = asyncio.run(_drive())
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "ready"


def test_exposed_route_runs_before_request_and_uses_g_and_current_app():
    """An exposed route reading `g` / `current_app` works over MCP, and an
    `@app.before_request` hook populating `g` runs before the handler."""
    from veloce import current_app, g

    app = Veloce(openapi_url=None)

    @app.before_request
    async def _populate(request):
        g.user = "ada"

    @app.get("/whoami", expose_as_mcp_tool=True, mcp_description="Current user")
    async def whoami() -> dict:
        # Both `g` (set by before_request) and `current_app` must be bound.
        return {"user": g.user, "app": current_app.title}

    out = _call(app, "whoami", {})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload["user"] == "ada"
    assert payload["app"] == app.title


def test_before_request_short_circuit_becomes_iserror_result():
    """A `before_request` hook returning a 401 short-circuits the tool: the
    handler is not called and the denial surfaces as an isError result."""
    app = Veloce(openapi_url=None)
    called: list[str] = []

    @app.before_request
    async def _auth(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    @app.get("/secret", expose_as_mcp_tool=True, mcp_description="Protected")
    async def secret() -> dict:
        called.append("handler")
        return {"ok": True}

    out = _call(app, "secret", {})
    assert "error" not in out  # not a JSON-RPC transport error
    assert out["result"]["isError"] is True
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"detail": "unauthorized"}
    # The handler never ran - the hook short-circuited the call.
    assert called == []


def test_dependency_typed_mcpcontext_receives_context():
    """A sub-dependency declaring `ctx: MCPContext` receives the per-call
    context, not a missing-argument invalid-params error."""
    app = Veloce(openapi_url=None)

    def dep(ctx: MCPContext) -> str:
        return ctx.tool_name

    @app.mcp_tool(description="Read the tool name via a dependency")
    async def via_dep(name: str = Depends(dep)) -> str:
        return name

    # The MCPContext sub-dependency is not an agent input.
    registry = build_registry(app)
    assert "name" not in registry.tools["via_dep"].input_schema["properties"]

    out = _call(app, "via_dep", {})
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "via_dep"


def test_exposed_route_streaming_response_is_buffered():
    """A route returning a StreamingResponse is drained into a single tool result
    rather than rejected."""
    from veloce import StreamingResponse

    app = Veloce(openapi_url=None)

    @app.get("/stream", expose_as_mcp_tool=True, mcp_description="Stream chunks")
    async def stream() -> StreamingResponse:
        async def gen():
            yield b"hello "
            yield b"world"

        return StreamingResponse(gen(), content_type="text/plain")

    out = _call(app, "stream", {})
    assert "error" not in out
    assert out["result"].get("isError") is not True
    assert out["result"]["content"][0]["text"] == "hello world"


def test_exposed_route_streaming_json_is_decoded():
    """A streamed JSON body is buffered and decoded back to a value."""
    from veloce import StreamingResponse

    app = Veloce(openapi_url=None)

    @app.get("/numbers", expose_as_mcp_tool=True, mcp_description="Stream JSON")
    async def numbers() -> StreamingResponse:
        async def gen():
            yield b'{"nums":'
            yield b"[1,2,3]}"

        return StreamingResponse(gen(), content_type="application/json")

    out = _call(app, "numbers", {})
    assert "error" not in out
    assert orjson.loads(out["result"]["content"][0]["text"]) == {"nums": [1, 2, 3]}


def test_exposed_route_sse_response_is_buffered():
    """An EventSourceResponse is drained into its SSE-framed text."""
    from veloce import EventSourceResponse, ServerSentEvent

    app = Veloce(openapi_url=None)

    @app.get("/events", expose_as_mcp_tool=True, mcp_description="Stream events")
    async def events() -> EventSourceResponse:
        async def gen():
            yield ServerSentEvent(data="one")
            yield ServerSentEvent(data="two")

        return EventSourceResponse(gen())

    out = _call(app, "events", {})
    assert "error" not in out
    text = out["result"]["content"][0]["text"]
    assert "data: one" in text
    assert "data: two" in text


def test_pure_tool_streaming_response_is_buffered():
    """A pure `@app.mcp_tool` returning a StreamingResponse is buffered too."""
    from veloce import StreamingResponse

    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Stream chunks")
    async def stream() -> StreamingResponse:
        async def gen():
            yield b"chunk-1|"
            yield b"chunk-2"

        return StreamingResponse(gen(), content_type="text/plain")

    out = _call(app, "stream", {})
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "chunk-1|chunk-2"


def test_streaming_response_over_buffer_limit_is_in_band_error(monkeypatch):
    """A stream past the buffer limit yields an in-band error, not unbounded use."""
    from veloce import StreamingResponse
    from veloce.contrib.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "_STREAM_BUFFER_LIMIT", 8)

    app = Veloce(openapi_url=None)

    @app.get("/big", expose_as_mcp_tool=True, mcp_description="Oversized stream")
    async def big() -> StreamingResponse:
        async def gen():
            yield b"0123456789ABCDEF"

        return StreamingResponse(gen(), content_type="text/plain")

    out = _call(app, "big", {})
    assert "error" not in out  # in-band tool error, not a transport error
    assert out["result"]["isError"] is True
    assert "buffer limit" in out["result"]["content"][0]["text"]


def test_handler_response_background_task_runs():
    """A handler returning `Response(background=BackgroundTask(fn))` runs fn,
    mirroring the HTTP path's response-attached background execution."""
    from veloce.background import BackgroundTask

    app = Veloce(openapi_url=None)
    ran: list[str] = []

    async def side_effect() -> None:
        ran.append("done")

    @app.get("/with-bg", expose_as_mcp_tool=True, mcp_description="Response with bg task")
    async def with_bg() -> Response:
        return Response(
            body=b"queued",
            content_type="text/plain",
            background=BackgroundTask(side_effect),
        )

    out = _call(app, "with_bg", {})
    assert "error" not in out
    # The route-derived tool unwraps the Response body, mirroring the HTTP path.
    assert out["result"]["content"][0]["text"] == "queued"
    # The response-attached background task ran after the handler returned.
    assert ran == ["done"]


def test_typed_context_still_injected():
    """A parameter typed `MCPContext` still receives the injected context even
    when named `ctx`, and is not an agent input."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Return the tool name from a typed context")
    async def named(ctx: MCPContext) -> str:
        return ctx.tool_name

    registry = build_registry(app)
    assert "ctx" not in registry.tools["named"].input_schema["properties"]

    out = _call(app, "named", {})
    assert out["result"]["content"][0]["text"] == "named"


# -- Route-backed tool request lifecycle ------------------------------


def test_exposed_route_exception_routes_through_exception_handler():
    """A route raising `HTTPException` goes through the app's exception handlers
    over MCP, so the registered handler's body is the tool result (isError),
    not a `str(exc)` repr."""
    app = Veloce(openapi_url=None)

    @app.exception_handler(HTTPException)
    async def _handle(request, exc):
        return JSONResponse(
            {"error": exc.detail, "code": exc.status_code}, status_code=exc.status_code
        )

    @app.get("/boom", expose_as_mcp_tool=True, mcp_description="Always fails")
    async def boom() -> dict:
        raise HTTPException(status_code=418, detail="teapot")

    out = _call(app, "boom", {})
    assert "error" not in out  # in-band tool error, not a JSON-RPC transport error
    assert out["result"]["isError"] is True
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"error": "teapot", "code": 418}


def test_exposed_route_httpexception_default_body():
    """With no registered handler, a route raising `HTTPException` still yields
    the framework's default JSON error body (not `str(exc)`)."""
    app = Veloce(openapi_url=None)

    @app.get("/missing", expose_as_mcp_tool=True, mcp_description="Not found")
    async def missing() -> dict:
        raise HTTPException(status_code=404, detail="nope")

    out = _call(app, "missing", {})
    assert "error" not in out
    assert out["result"]["isError"] is True
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"detail": "nope"}


def test_exposed_route_path_param_visible_in_dependency():
    """A tool argument naming a route path parameter lands on
    `request.path_params`, so a dependency / hook reading it sees the value."""
    app = Veloce(openapi_url=None)
    seen: dict[str, object] = {}

    def read_path_param(request) -> int:
        # The HTTP path fills this from URL segments; over MCP it must come from
        # the tool arguments that name a path parameter. The value carries the
        # client's JSON type (an int here), not a re-stringified URL segment.
        seen["params"] = dict(request.path_params)
        return request.path_params["item_id"]

    @app.get("/items/{item_id}", expose_as_mcp_tool=True, mcp_description="Get an item")
    async def get_item(item_id: int, pp: int = Depends(read_path_param)) -> dict:
        return {"item_id": item_id, "from_path_params": pp}

    out = _call(app, "get_item", {"item_id": 7})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"item_id": 7, "from_path_params": 7}
    assert seen["params"] == {"item_id": 7}


def test_exposed_route_after_request_rewrite_reflected_in_result():
    """An `@app.after_request` hook that replaces the response is honoured over
    MCP, so the rewritten body is the tool result."""
    app = Veloce(openapi_url=None)

    @app.after_request
    async def _rewrite(request, response):
        # Replace the handler's response entirely - the tool result must follow.
        return JSONResponse({"rewritten": True})

    @app.get("/orig", expose_as_mcp_tool=True, mcp_description="Original")
    async def orig() -> dict:
        return {"rewritten": False}

    out = _call(app, "orig", {})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"rewritten": True}


def test_exposed_route_teardown_request_runs_on_success_and_failure():
    """`@app.teardown_request` runs for a route-backed tool and receives the
    exception on failure (and `None` on success), mirroring the HTTP path."""
    app = Veloce(openapi_url=None)
    torn: list[object] = []

    @app.teardown_request
    def _teardown(exc):
        torn.append(exc)

    @app.get("/ok", expose_as_mcp_tool=True, mcp_description="Succeeds")
    async def ok() -> dict:
        return {"ok": True}

    @app.get("/fail", expose_as_mcp_tool=True, mcp_description="Fails")
    async def fail() -> dict:
        raise RuntimeError("kaboom")

    out_ok = _call(app, "ok", {})
    assert "error" not in out_ok
    assert torn == [None]

    torn.clear()
    out_fail = _call(app, "fail", {})
    # A non-HTTP exception with no handler falls back to the framework 500 body.
    assert "error" not in out_fail
    assert out_fail["result"]["isError"] is True
    assert len(torn) == 1
    assert isinstance(torn[0], RuntimeError)
    assert str(torn[0]) == "kaboom"


def test_exposed_route_teardown_appcontext_runs():
    """`@app.teardown_appcontext` fires for a route-backed tool call."""
    app = Veloce(openapi_url=None)
    torn: list[object] = []

    @app.teardown_appcontext
    def _teardown(exc):
        torn.append(exc)

    @app.get("/ac", expose_as_mcp_tool=True, mcp_description="App context")
    async def ac() -> dict:
        return {"ok": True}

    out = _call(app, "ac", {})
    assert "error" not in out
    assert torn == [None]


# -- Full request-lifecycle fidelity for route-backed tools -----------


def test_sub_dependency_query_marker_resolves_from_tool_args():
    """A sub-dependency parameter declared `user_id: int = Query(...)` resolves
    from the tool arguments, the same way a top-level `Query` tool param does -
    not from the empty synthetic request (which would raise missing-parameter).
    The value also keeps its coerced type (an `int`, not the raw string)."""
    from veloce import Query

    app = Veloce(openapi_url=None)

    def lookup(user_id: int = Query(...)) -> int:
        # Reads from the same `arguments` a top-level `Query` param would.
        return user_id * 2

    @app.get("/dbl", expose_as_mcp_tool=True, mcp_description="Double a user id")
    async def dbl(doubled: int = Depends(lookup)) -> dict:
        return {"doubled": doubled}

    out = _call(app, "dbl", {"user_id": 21})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"doubled": 42}


def test_sub_dependency_body_model_resolves_from_tool_args():
    """A sub-dependency declaring a Pydantic body model (`item: Item`) validates
    against the tool arguments, mirroring how the HTTP JSON body feeds a body
    model declared inside a `Depends` sub-plan."""
    app = Veloce(openapi_url=None)

    def parse(item: Item) -> str:
        return f"{item.name} x{item.qty}"

    @app.post("/mk", expose_as_mcp_tool=True, mcp_description="Make an item")
    async def mk(label: str = Depends(parse)) -> dict:
        return {"label": label}

    out = _call(app, "mk", {"name": "widget", "qty": 3})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"label": "widget x3"}


def test_sub_dependency_query_marker_coercion_failure_is_invalid_params():
    """A coercion failure resolving a sub-dependency marker maps to the
    JSON-RPC invalid-params error channel, as a prior round established for
    top-level markers."""
    from veloce import Query

    app = Veloce(openapi_url=None)

    def lookup(count: int = Query(...)) -> int:
        return count

    @app.get("/cnt", expose_as_mcp_tool=True, mcp_description="Count")
    async def cnt(n: int = Depends(lookup)) -> dict:
        return {"n": n}

    out = _call(app, "cnt", {"count": "not-an-int"})
    assert "result" not in out
    assert out["error"]["code"] == -32602


def test_before_request_short_circuit_still_runs_teardown_request():
    """A `before_request` hook returning a Response short-circuits the tool, but
    `teardown_request` must still run (with `exc=None`) - the HTTP path runs
    teardown even when `before_request` returns early."""
    app = Veloce(openapi_url=None)
    torn: list[object] = []

    @app.teardown_request
    def _teardown(exc):
        torn.append(exc)

    @app.before_request
    async def _deny(request):
        return JSONResponse({"detail": "nope"}, status_code=401)

    @app.get("/guarded", expose_as_mcp_tool=True, mcp_description="Guarded")
    async def guarded() -> dict:
        return {"ok": True}

    out = _call(app, "guarded", {})
    assert "error" not in out
    assert out["result"]["isError"] is True
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"detail": "nope"}
    # Teardown fired despite the short-circuit, receiving None (no exception).
    assert torn == [None]


def test_dependency_injected_response_mutation_reflected_in_result():
    """A dependency that injects `response: Response` and mutates it (status +
    header) shares the request-scoped injected Response with the route path's
    `_build_response`, so the mutation is reflected in the tool result."""
    app = Veloce(openapi_url=None)

    def stamp(response: Response) -> None:
        response.status_code = 418
        response.headers["X-Stamp"] = "on"

    @app.get("/stamped", expose_as_mcp_tool=True, mcp_description="Stamped")
    async def stamped(_: None = Depends(stamp)) -> dict:
        return {"ok": True}

    out = _call(app, "stamped", {})
    assert "error" not in out
    # The injected 418 was merged onto the final response, so a >= 400 status
    # surfaces as an in-band tool error.
    assert out["result"]["isError"] is True
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"ok": True}


def test_shared_background_tasks_queue_runs_dependency_and_handler_tasks():
    """A dependency that injects `BackgroundTasks` and a handler that also takes
    `BackgroundTasks` share one request-scoped queue, so BOTH scheduled tasks
    run - the handler's injection must not discard the dependency's work."""
    app = Veloce(openapi_url=None)
    ran: list[str] = []

    async def dep_task() -> None:
        ran.append("dep")

    async def handler_task() -> None:
        ran.append("handler")

    def schedule_dep(tasks: BackgroundTasks) -> str:
        tasks.add_task(dep_task)
        return "scheduled"

    @app.get("/bg2", expose_as_mcp_tool=True, mcp_description="Two bg tasks")
    async def bg2(tasks: BackgroundTasks, _: str = Depends(schedule_dep)) -> dict:
        tasks.add_task(handler_task)
        return {"ok": True}

    out = _call(app, "bg2", {})
    assert "error" not in out
    # Both tasks ran - the queue was shared, not overwritten by the handler slot.
    assert sorted(ran) == ["dep", "handler"]


def test_url_value_preprocessor_observed_over_mcp():
    """A `url_value_preprocessor` that rewrites a path param and seeds `g` runs
    for a route-backed tool over MCP, so a dependency reading
    `request.path_params` sees the rewrite and the handler reads the seeded
    `g` value - exactly as on the HTTP path."""
    from veloce import g

    app = Veloce(openapi_url=None)

    @app.url_value_preprocessor
    def pull_lang(endpoint, values):
        # Rewrite the captured path param and stash a value on `g`, the locale
        # / tenant extraction pattern preprocessors exist for.
        values["item_id"] = values["item_id"] + 100
        g.lang = "en"

    def read_param(request) -> int:
        # The preprocessor rewrote `path_params` before the handler graph
        # resolved, so this dependency observes the rewritten value.
        return request.path_params["item_id"]

    @app.get("/loc/{item_id}", expose_as_mcp_tool=True, mcp_description="Localised")
    async def loc(rewritten: int = Depends(read_param)) -> dict:
        return {"item_id": rewritten, "lang": g.lang}

    out = _call(app, "loc", {"item_id": 7})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    # 7 + 100 from the preprocessor's rewrite, and `g.lang` seeded by it.
    assert payload == {"item_id": 107, "lang": "en"}


# -- HTTP-route alignment: schema, method/path, middleware, defaults, status --


def test_sub_dependency_query_param_advertised_in_input_schema():
    """`tools/list` must advertise a sub-dependency's `Query` param as a tool
    input. The schema is what the agent reads to know which arguments to send;
    omitting it would advertise no inputs while `tools/call` rejected the call
    with invalid-params unless the value was supplied."""
    from veloce import Query

    app = Veloce(openapi_url=None)

    def lookup(user_id: int = Query(...)) -> int:
        return user_id

    @app.get("/u", expose_as_mcp_tool=True, mcp_description="Look a user up")
    async def u(found: int = Depends(lookup)) -> dict:
        return {"found": found}

    schema = build_registry(app).tools["u"].input_schema
    # The sub-dependency's client-supplied param surfaces as a top-level input.
    assert "user_id" in schema["properties"]
    assert schema["properties"]["user_id"]["type"] == "integer"
    assert "user_id" in schema["required"]
    # The `Depends` slot itself (`found`) is never an input.
    assert "found" not in schema["properties"]


def test_dependency_body_model_fields_advertised_in_input_schema():
    """A body model declared inside a `Depends` sub-dependency contributes its
    fields to the tool input schema, mirroring how a top-level body model does;
    the model is inlined under `$defs` so the schema stays self-contained."""
    app = Veloce(openapi_url=None)

    def parse(item: Item) -> str:
        return item.name

    @app.post("/mk2", expose_as_mcp_tool=True, mcp_description="Make from a dep model")
    async def mk2(label: str = Depends(parse)) -> dict:
        return {"label": label}

    schema = build_registry(app).tools["mk2"].input_schema
    # The dependency's body model surfaces as an input property referencing the
    # inlined `Item` def, whose fields (name, qty) are resolvable in-schema.
    assert "item" in schema["properties"]
    assert "Item" in schema.get("$defs", {})
    item_def = schema["$defs"]["Item"]
    assert set(item_def["properties"]) == {"name", "qty"}


def test_route_backed_tool_sees_route_method_and_path():
    """A route-backed tool's handler and a dependency must see the wrapped
    route's real HTTP method and rule path on `request`, not the synthetic
    `"MCP"` / `/mcp/<tool>` values - routes/deps that branch on
    `request.method` / `request.path` then behave as on the HTTP path."""
    app = Veloce(openapi_url=None)

    def read_method(request) -> str:
        # A dependency observing the request branches on the real verb.
        return request.method

    @app.post("/items/{item_id}", expose_as_mcp_tool=True, mcp_description="Make item")
    async def make_item(item_id: int, request, seen: str = Depends(read_method)) -> dict:
        return {"method": request.method, "path": request.path, "dep_method": seen}

    out = _call(app, "make_item", {"item_id": 5})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload["method"] == "POST"
    # `request.path` is the rule pattern; the concrete id lives in path_params.
    assert payload["path"] == "/items/{item_id}"
    assert payload["dep_method"] == "POST"


def test_request_middleware_process_request_runs_on_mcp_call():
    """An app `Middleware.process_request` runs for a route-backed MCP call, so
    a route depending on middleware-populated state behaves as it does over
    HTTP. The middleware here stamps `request.state`, which the handler reads."""
    from veloce.middleware.base import Middleware

    app = Veloce(openapi_url=None)

    class StampMiddleware(Middleware):
        async def process_request(self, request):
            request.state.stamp = "mw"
            return None

    app.add_middleware(StampMiddleware)

    @app.get("/stamped2", expose_as_mcp_tool=True, mcp_description="Stamped by mw")
    async def stamped2(request) -> dict:
        return {"stamp": getattr(request.state, "stamp", None)}

    out = _call(app, "stamped2", {})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"stamp": "mw"}


def test_request_middleware_short_circuit_returns_response_and_runs_teardown():
    """A request middleware that short-circuits by returning a `Response` ends
    the MCP call with that response (shaped to an isError result), the handler
    never runs, and `teardown_request` still fires - mirroring the HTTP path."""
    from veloce.middleware.base import Middleware

    app = Veloce(openapi_url=None)
    called: list[str] = []
    torn: list[object] = []

    @app.teardown_request
    def _teardown(exc):
        torn.append(exc)

    class DenyMiddleware(Middleware):
        async def process_request(self, request):
            return JSONResponse({"detail": "blocked"}, status_code=403)

    app.add_middleware(DenyMiddleware)

    @app.get("/mwsecret", expose_as_mcp_tool=True, mcp_description="MW guarded")
    async def mwsecret() -> dict:
        called.append("handler")
        return {"ok": True}

    out = _call(app, "mwsecret", {})
    assert "error" not in out  # not a JSON-RPC transport error
    assert out["result"]["isError"] is True
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"detail": "blocked"}
    # The handler was never reached, and teardown ran despite the short-circuit.
    assert called == []
    assert torn == [None]


def test_exclude_middleware_skips_middleware_on_mcp_call():
    """A route declaring `exclude_middleware=[...]` must skip the named
    middleware over MCP exactly as on the HTTP path. A short-circuiting excluded
    middleware therefore does NOT block the tool call, while a non-excluded
    middleware still runs and its effect is observable in the result."""
    from veloce.middleware.base import Middleware

    app = Veloce(openapi_url=None)
    blocked: list[str] = []

    class Blocker(Middleware):
        async def process_request(self, request):
            # If this ran, the call would short-circuit with 403 and the handler
            # would never execute. The route excludes it, so it must not run.
            blocked.append("blocker")
            return JSONResponse({"detail": "blocked"}, status_code=403)

    class Stamper(Middleware):
        async def process_request(self, request):
            request.state.stamp = "stamped"
            return None

    app.add_middleware(Blocker)
    app.add_middleware(Stamper)

    @app.get(
        "/open-tool",
        expose_as_mcp_tool=True,
        mcp_description="Open tool",
        exclude_middleware=["Blocker"],
    )
    async def open_tool(request) -> dict:
        return {"stamp": getattr(request.state, "stamp", None)}

    out = _call(app, "open_tool", {})
    assert "error" not in out
    # The excluded Blocker did not short-circuit; the call succeeded.
    assert out["result"].get("isError") is not True
    payload = orjson.loads(out["result"]["content"][0]["text"])
    # The non-excluded Stamper still ran and stamped the request state.
    assert payload == {"stamp": "stamped"}
    assert blocked == []


def test_route_defaults_fill_unsupplied_mcp_argument():
    """A route with `defaults={'mode': 'summary'}` is callable over MCP without
    the agent supplying `mode`: the route default fills the handler kwarg, as
    the HTTP path merges defaults into the dispatch params."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/dash",
        defaults={"mode": "summary"},
        expose_as_mcp_tool=True,
        mcp_description="Dashboard",
    )
    async def dash(mode: str) -> dict:
        return {"mode": mode}

    # `mode` is not supplied; the route default must fill it.
    out = _call(app, "dash", {})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"mode": "summary"}

    # An explicit argument still wins over the route default.
    out2 = _call(app, "dash", {"mode": "detail"})
    payload2 = orjson.loads(out2["result"]["content"][0]["text"])
    assert payload2 == {"mode": "detail"}


def test_instrumentation_records_real_status_for_short_circuit_and_error():
    """Instrumentation must record the call's real status, not a hard-coded 200:
    a 401 `before_request` short-circuit reports 401, and a route handler that
    raises (routed through the default `HTTPException`/500 path) reports 500."""
    app = Veloce(openapi_url=None)
    seen: list[int] = []

    @app.add_instrumentation
    def record(metrics):
        seen.append(metrics.status_code)

    @app.before_request
    async def _auth(request):
        if request.endpoint == "denied":
            return JSONResponse({"detail": "no"}, status_code=401)
        return None

    @app.get("/denied", expose_as_mcp_tool=True, mcp_description="Denied")
    async def denied() -> dict:
        return {"ok": True}

    @app.get("/boom", expose_as_mcp_tool=True, mcp_description="Boom")
    async def boom() -> dict:
        raise RuntimeError("kaboom")

    _call(app, "denied", {})
    _call(app, "boom", {})
    # The 401 short-circuit and the 500 from the unhandled error are both
    # reported with their real status, never collapsed to 200.
    assert 401 in seen
    assert 500 in seen
    assert 200 not in seen


# -- Protocol version + ping ------------------------------------------


def _initialize(app: Veloce, params: dict) -> dict:
    """Drive one `initialize` and return the response object."""
    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    return asyncio.run(pipe.run())[0]


def _list_tools(app: Veloce) -> dict[str, dict]:
    """Drive one `tools/list` and return the entries keyed by tool name."""
    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    out = asyncio.run(pipe.run())[0]
    return {tool["name"]: tool for tool in out["result"]["tools"]}


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
    pipe = _Pipe(_server(app))
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


# -- Output schema + structured content -------------------------------


def test_output_schema_from_response_model():
    """A route `response_model` produces a standalone object output schema."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/me",
        response_model=PublicUser,
        expose_as_mcp_tool=True,
        mcp_description="Current user",
    )
    async def me() -> dict:
        return {"id": 1, "name": "ada"}

    schema = _list_tools(app)["me"]["outputSchema"]
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"id", "name"}


def test_output_schema_inlines_nested_defs():
    """A nested model in the output schema is inlined under `$defs`, standalone."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/customer",
        response_model=Customer,
        expose_as_mcp_tool=True,
        mcp_description="A customer",
    )
    async def customer() -> dict:
        return {"name": "ada", "address": {"city": "x", "zip": "1"}}

    schema = _list_tools(app)["customer"]["outputSchema"]
    assert "Address" in schema["$defs"]
    # No OpenAPI envelope refs leak into a standalone MCP schema.
    assert "#/components/schemas/" not in orjson.dumps(schema).decode()


def test_scalar_tool_has_no_output_schema():
    """A scalar / non-model return declares no output schema."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    assert "outputSchema" not in _list_tools(app)["add"]


def test_structured_content_for_object_result():
    """A tool with an output schema returns `structuredContent` alongside text."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/me",
        response_model=PublicUser,
        expose_as_mcp_tool=True,
        mcp_description="Current user",
    )
    async def me() -> dict:
        return {"id": 7, "name": "ada"}

    result = _call(app, "me", {})["result"]
    assert result["structuredContent"] == {"id": 7, "name": "ada"}
    # The text content block is still present for back-compatibility.
    assert orjson.loads(result["content"][0]["text"]) == {"id": 7, "name": "ada"}


def test_no_structured_content_without_output_schema():
    """A scalar tool (no output schema) returns only the text content block."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    result = _call(app, "add", {"a": 2, "b": 3})["result"]
    assert "structuredContent" not in result
    assert result["content"][0]["text"] == "5"


def test_error_result_has_no_structured_content():
    """A 4xx in-band error surfaces text + isError, never structuredContent."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/me",
        response_model=PublicUser,
        expose_as_mcp_tool=True,
        mcp_description="Current user",
    )
    async def me() -> dict:
        raise HTTPException(status_code=404, detail="gone")

    result = _call(app, "me", {})["result"]
    assert result["isError"] is True
    assert "structuredContent" not in result


def test_response_model_route_returning_response_emits_filtered_structured_content():
    """A response_model route whose handler returns its own Response still emits
    structuredContent, re-filtered through response_model so it conforms to the
    advertised outputSchema and hidden fields do not leak."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/users",
        response_model=PublicUser,
        expose_as_mcp_tool=True,
        mcp_description="List users",
    )
    async def users() -> JSONResponse:
        # The handler builds its own Response, bypassing the response_model
        # filter; the body even carries a hidden field the model excludes.
        return JSONResponse({"id": 7, "name": "ada", "password": "secret"})

    # tools/list advertises the outputSchema (derived from response_model).
    assert "outputSchema" in _list_tools(app)["users"]

    result = _call(app, "users", {})["result"]
    # The MCP spec requires conforming structuredContent when an outputSchema is
    # declared; the body is re-run through response_model so the hidden field is
    # filtered out while the contract is honoured.
    assert result["structuredContent"] == {"id": 7, "name": "ada"}
    assert "password" not in result["structuredContent"]
    assert result.get("isError") is not True


def test_response_model_route_streaming_response_emits_filtered_structured_content():
    """A streamed Response on a response_model route is buffered, decoded, and
    re-filtered through response_model so structuredContent conforms."""
    from veloce import StreamingResponse

    app = Veloce(openapi_url=None)

    @app.get(
        "/stream-user",
        response_model=PublicUser,
        expose_as_mcp_tool=True,
        mcp_description="Stream a user",
    )
    async def stream_user() -> StreamingResponse:
        async def gen():
            yield b'{"id": 7, "name": "ada", "password": "secret"}'

        return StreamingResponse(gen(), content_type="application/json")

    result = _call(app, "stream_user", {})["result"]
    assert result["structuredContent"] == {"id": 7, "name": "ada"}
    assert "password" not in result["structuredContent"]
    assert result.get("isError") is not True


def test_recursive_response_model_output_schema_resolves():
    """A self-referential response_model yields a resolvable outputSchema whose
    $defs entry retains the model's real object body."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/tree",
        response_model=Node,
        expose_as_mcp_tool=True,
        mcp_description="Get a node tree",
    )
    async def tree() -> Node:
        return Node(name="root", children=[])

    schema = _list_tools(app)["tree"]["outputSchema"]
    node_def = schema["$defs"]["Node"]
    # The real object body must survive - not be clobbered by a bare self-$ref.
    assert "properties" in node_def
    assert "name" in node_def["properties"]
    assert "children" in node_def["properties"]


def test_output_schema_renders_serialization_mode():
    """Every key the structured result carries appears in `outputSchema`.

    A computed field surfaces in the serialization-mode dump but not the
    validation-mode schema; rendering the output schema in serialization mode
    keeps `structuredContent` conformant to its `outputSchema`.
    """
    app = Veloce(openapi_url=None)

    @app.get(
        "/computed",
        response_model=ComputedOut,
        expose_as_mcp_tool=True,
        mcp_description="Computed output",
    )
    async def computed() -> ComputedOut:
        return ComputedOut(a=5)

    schema = _list_tools(app)["computed"]["outputSchema"]
    result = _call(app, "computed", {})["result"]
    structured = result["structuredContent"]
    # The computed field reaches the client.
    assert structured == {"a": 5, "b": 6}
    # Every emitted key is declared in the advertised output schema.
    for key in structured:
        assert key in schema["properties"], key


def test_pure_tool_nonconforming_return_is_in_band_error():
    """A pure tool whose return does not match its declared model yields an
    in-band error rather than advertising a schema and emitting non-conforming
    (or absent) structured content."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Claims a model but returns a scalar")
    async def bad() -> PublicUser:  # type: ignore[return-value]
        return 5  # type: ignore[return-value]

    # The schema is advertised from the declared return type.
    assert "outputSchema" in _list_tools(app)["bad"]

    result = _call(app, "bad", {})["result"]
    assert result["isError"] is True
    assert "structuredContent" not in result


def test_pure_tool_conforming_dict_return_is_validated():
    """A pure tool returning a dict that conforms is coerced through its model so
    `structuredContent` is the serialization-mode dump."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Returns a conforming dict")
    async def good() -> PublicUser:  # type: ignore[return-value]
        return {"id": 3, "name": "ada", "extra": "dropped"}  # type: ignore[return-value]

    result = _call(app, "good", {})["result"]
    assert result["structuredContent"] == {"id": 3, "name": "ada"}
    assert result.get("isError") is not True


def test_response_model_route_plaintext_response_does_not_crash():
    """A response_model route whose handler returns a non-JSON `Response` emits a
    text block without crashing into a JSON-RPC transport error."""
    from veloce import PlainTextResponse

    app = Veloce(openapi_url=None)

    @app.get(
        "/text",
        response_model=PublicUser,
        expose_as_mcp_tool=True,
        mcp_description="Returns plain text",
    )
    async def text() -> PlainTextResponse:
        return PlainTextResponse("hello")

    response = _call(app, "text", {})
    # Must not be a JSON-RPC transport error.
    assert "error" not in response
    result = response["result"]
    assert result.get("isError") is not True
    assert result["content"][0]["text"] == "hello"
    assert "structuredContent" not in result


def test_response_model_route_sse_response_does_not_crash():
    """A response_model route whose handler returns an SSE stream is drained and
    emitted as text without crashing the re-filter."""
    from veloce import EventSourceResponse, ServerSentEvent

    app = Veloce(openapi_url=None)

    @app.get(
        "/sse",
        response_model=PublicUser,
        expose_as_mcp_tool=True,
        mcp_description="Returns an SSE stream",
    )
    async def sse() -> EventSourceResponse:
        async def gen():
            yield ServerSentEvent(data="tick")

        return EventSourceResponse(gen())

    response = _call(app, "sse", {})
    assert "error" not in response
    result = response["result"]
    assert result.get("isError") is not True
    assert "structuredContent" not in result


def test_slow_stream_times_out_in_band(monkeypatch):
    """A streamed result that does not complete within the drain budget yields an
    in-band error and does not hang the serve loop."""
    from veloce import StreamingResponse
    from veloce.contrib.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "_STREAM_DRAIN_TIMEOUT", 0.05)

    app = Veloce(openapi_url=None)

    @app.get("/slow", expose_as_mcp_tool=True, mcp_description="Slow stream")
    async def slow() -> StreamingResponse:
        async def gen():
            yield b"start"
            # A small, never-completing feed: under the size cap forever.
            while True:
                await asyncio.sleep(0.01)
                yield b"."

        return StreamingResponse(gen(), content_type="text/plain")

    result = _call(app, "slow", {})["result"]
    assert result["isError"] is True
    assert "timeout" in result["content"][0]["text"].lower()


def test_oversized_stream_closes_producer_promptly(monkeypatch):
    """The size-cap path closes the producing generator so its finally runs
    immediately, not only at GC."""
    from veloce import StreamingResponse
    from veloce.contrib.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "_STREAM_BUFFER_LIMIT", 8)

    app = Veloce(openapi_url=None)
    closed = []

    @app.get("/big", expose_as_mcp_tool=True, mcp_description="Oversized stream")
    async def big() -> StreamingResponse:
        async def gen():
            try:
                yield b"0123456789ABCDEF"
                yield b"more"
            finally:
                closed.append(True)

        return StreamingResponse(gen(), content_type="text/plain")

    result = _call(app, "big", {})["result"]
    assert result["isError"] is True
    assert "buffer limit" in result["content"][0]["text"]
    # The producer's finally ran promptly, not deferred to GC.
    assert closed == [True]


def test_slow_stream_with_awaiting_cleanup_stays_in_budget(monkeypatch):
    """A generator whose teardown awaits cannot re-wedge the serve loop past the
    drain deadline: the cleanup aclose() is itself bounded."""
    from veloce import StreamingResponse
    from veloce.contrib.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "_STREAM_DRAIN_TIMEOUT", 0.05)

    app = Veloce(openapi_url=None)

    @app.get("/slow", expose_as_mcp_tool=True, mcp_description="Slow stream")
    async def slow() -> StreamingResponse:
        async def gen():
            try:
                yield b"start"
                while True:
                    await asyncio.sleep(0.01)
                    yield b"."
            finally:
                # An adversarial teardown that awaits far past the deadline; the
                # bounded cleanup must not let it block the result.
                await asyncio.sleep(10)

        return StreamingResponse(gen(), content_type="text/plain")

    started = time.perf_counter()
    result = _call(app, "slow", {})["result"]
    elapsed = time.perf_counter() - started
    assert result["isError"] is True
    assert "timeout" in result["content"][0]["text"].lower()
    # Drain budget + cleanup budget, with generous slack for scheduling - far
    # under the 10s the un-bounded teardown would have added.
    assert elapsed < 2.0


def test_multi_verb_route_annotations_are_conservative():
    """A route serving several verbs is rated across all of them, not just the
    first yielded verb."""
    app = Veloce(openapi_url=None)

    @app.route(
        "/items/{n}",
        methods=["GET", "DELETE"],
        expose_as_mcp_tool=True,
        mcp_description="Fetch or delete an item",
    )
    async def items(n: int) -> dict:
        return {"n": n}

    ann = _list_tools(app)["items"]["annotations"]
    # GET alone would read read-only / non-destructive, but DELETE makes the
    # tool neither: the conservative rating flags it across both verbs.
    assert ann["readOnlyHint"] is False
    assert ann["idempotentHint"] is True
    assert ann["destructiveHint"] is True


def test_multi_verb_additive_route_is_not_destructive():
    """A GET+POST route is mutating but additive, so not flagged destructive."""
    app = Veloce(openapi_url=None)

    @app.route(
        "/items",
        methods=["GET", "POST"],
        expose_as_mcp_tool=True,
        mcp_description="List or create items",
    )
    async def items() -> dict:
        return {"ok": True}

    ann = _list_tools(app)["items"]["annotations"]
    assert ann["readOnlyHint"] is False
    assert ann["idempotentHint"] is False
    assert ann["destructiveHint"] is False


def test_output_schema_and_structured_content_agree_on_aliases():
    """The advertised outputSchema keys match the structuredContent keys for an
    aliased model, so the structured value conforms to its schema."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/aliased",
        response_model=AliasedOut,
        expose_as_mcp_tool=True,
        mcp_description="Aliased output",
    )
    async def aliased() -> dict:
        return {"userId": 7, "name": "ada"}

    tool = _list_tools(app)["aliased"]
    schema_keys = set(tool["outputSchema"]["properties"])
    structured = _call(app, "aliased", {})["result"]["structuredContent"]
    # response_model_by_alias defaults to False, so both the schema and the dump
    # use field names - and every structured key is in the schema.
    assert set(structured) <= schema_keys
    assert schema_keys == {"user_id", "name"}
    assert structured == {"user_id": 7, "name": "ada"}


def test_return_annotation_route_filters_handler_response_body():
    """A route typed only by its return annotation (no response_model) whose
    handler builds its own Response still drops fields outside the model."""
    app = Veloce(openapi_url=None)

    @app.get("/me_resp", expose_as_mcp_tool=True, mcp_description="Current user")
    async def me_resp() -> AnnotatedOut:
        return JSONResponse({"id": 1, "name": "ada", "secret": "x"})

    result = _call(app, "me_resp", {})["result"]
    assert result["structuredContent"] == {"id": 1, "name": "ada"}
    assert "secret" not in result["structuredContent"]


def test_return_annotation_route_filters_raw_dict():
    """A route typed only by its return annotation drops extra fields from a raw
    dict return before emitting structuredContent."""
    app = Veloce(openapi_url=None)

    @app.get("/me_dict", expose_as_mcp_tool=True, mcp_description="Current user")
    async def me_dict() -> AnnotatedOut:
        return {"id": 2, "name": "grace", "secret": "y"}

    result = _call(app, "me_dict", {})["result"]
    assert result["structuredContent"] == {"id": 2, "name": "grace"}
    assert "secret" not in result["structuredContent"]


def test_protocol_version_2025_03_26_not_echoed():
    """2025-03-26 is not advertised as supported (it lacks outputSchema etc.), so
    a request for it falls back to the latest supported revision."""
    from veloce.contrib.mcp.server import LATEST_PROTOCOL_VERSION

    app = Veloce(openapi_url=None)
    resp = _initialize(app, {"protocolVersion": "2025-03-26"})
    assert resp["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


def test_response_model_exclude_drops_required_from_output_schema():
    """A route excluding a required field advertises no `required`, so the
    partial structuredContent still conforms to the outputSchema."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/partial",
        response_model=FullUser,
        response_model_exclude={"password"},
        expose_as_mcp_tool=True,
        mcp_description="User without password",
    )
    async def partial() -> dict:
        return {"id": 1, "name": "ada", "password": "secret"}

    tool = _list_tools(app)["partial"]
    # `required` is dropped because exclude makes presence conditional.
    assert "required" not in tool["outputSchema"]
    result = _call(app, "partial", {})["result"]
    assert result["structuredContent"] == {"id": 1, "name": "ada"}
    assert "password" not in result["structuredContent"]


# -- Resources --------------------------------------------------------


def _list_resources(app: Veloce) -> dict[str, dict]:
    """Drive one `resources/list` and return the entries keyed by URI."""
    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}})
    out = asyncio.run(pipe.run())[0]
    return {r["uri"]: r for r in out["result"]["resources"]}


def _list_resource_templates(app: Veloce) -> dict[str, dict]:
    """Drive one `resources/templates/list` and return the entries keyed by template."""
    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list", "params": {}})
    out = asyncio.run(pipe.run())[0]
    return {r["uriTemplate"]: r for r in out["result"]["resourceTemplates"]}


def _read_resource(app: Veloce, uri: str) -> dict:
    """Drive one `resources/read` and return the single response object."""
    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}})
    return asyncio.run(pipe.run())[0]


def test_static_resource_is_listed():
    app = Veloce(openapi_url=None)

    @app.get(
        "/settings",
        summary="App settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app/settings",
        mcp_description="The application settings",
    )
    async def settings() -> dict:
        return {"debug": False}

    listed = _list_resources(app)
    assert "config://app/settings" in listed
    entry = listed["config://app/settings"]
    assert entry["name"] == "settings"
    assert entry["title"] == "App settings"
    assert entry["description"] == "The application settings"
    # A static resource is not advertised as a template.
    assert "config://app/settings" not in _list_resource_templates(app)


def test_template_resource_is_listed_as_template():
    app = Veloce(openapi_url=None)

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://{user_id}",
        mcp_description="A user record",
    )
    async def user(user_id: int) -> dict:
        return {"id": user_id}

    templates = _list_resource_templates(app)
    assert "users://{user_id}" in templates
    # A template is not advertised under the concrete-URI list.
    assert _list_resources(app) == {}


def test_static_resource_read_returns_text_contents():
    app = Veloce(openapi_url=None)

    @app.get(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app/settings",
        mcp_description="The application settings",
    )
    async def settings() -> dict:
        return {"debug": False, "name": "veloce"}

    out = _read_resource(app, "config://app/settings")
    assert "error" not in out
    contents = out["result"]["contents"]
    assert len(contents) == 1
    entry = contents[0]
    assert entry["uri"] == "config://app/settings"
    assert orjson.loads(entry["text"]) == {"debug": False, "name": "veloce"}


def test_template_resource_read_invokes_handler_with_path_param():
    app = Veloce(openapi_url=None)

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://{user_id}",
        mcp_description="A user record",
    )
    async def user(user_id: int) -> dict:
        # The value arrives coerced to int, exactly as on the HTTP path.
        return {"id": user_id, "doubled": user_id * 2}

    out = _read_resource(app, "users://21")
    assert "error" not in out
    entry = out["result"]["contents"][0]
    assert entry["uri"] == "users://21"
    assert orjson.loads(entry["text"]) == {"id": 21, "doubled": 42}


def test_resource_read_unknown_uri_is_resource_not_found():
    app = Veloce(openapi_url=None)

    @app.get(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app/settings",
        mcp_description="Settings",
    )
    async def settings() -> dict:
        return {}

    out = _read_resource(app, "config://does/not/exist")
    assert out["error"]["code"] == -32002


def test_resource_read_route_404_is_resource_not_found():
    app = Veloce(openapi_url=None)

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://{user_id}",
        mcp_description="A user record",
    )
    async def user(user_id: int) -> dict:
        raise HTTPException(status_code=404, detail="no such user")

    out = _read_resource(app, "users://7")
    assert out["error"]["code"] == -32002


def test_resource_read_template_coercion_failure_is_invalid_params():
    app = Veloce(openapi_url=None)

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://{user_id}",
        mcp_description="A user record",
    )
    async def user(user_id: int) -> dict:
        return {"id": user_id}

    # `abc` cannot coerce to the `user_id: int` path parameter.
    out = _read_resource(app, "users://abc")
    assert out["error"]["code"] == -32602


def test_resource_read_runs_route_dependency_guard():
    app = Veloce(openapi_url=None)

    def deny() -> None:
        raise HTTPException(status_code=403, detail="forbidden")

    @app.get(
        "/secret",
        dependencies=[Depends(deny)],
        expose_as_mcp_resource=True,
        mcp_resource_uri="secret://data",
        mcp_description="Guarded data",
    )
    async def secret() -> dict:
        return {"top": "secret"}

    out = _read_resource(app, "secret://data")
    # The guard runs on the resource read, so the read fails rather than
    # returning the protected body.
    assert "error" in out
    assert "result" not in out


def test_initialize_advertises_resources_when_present():
    app = Veloce(openapi_url=None)

    @app.get(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
        mcp_description="Settings",
    )
    async def settings() -> dict:
        return {}

    caps = _initialize(app, {})["result"]["capabilities"]
    assert caps["resources"] == {"subscribe": False, "listChanged": False}


def test_initialize_omits_resources_capability_when_none():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    caps = _initialize(app, {})["result"]["capabilities"]
    assert "resources" not in caps


def test_resource_on_mutating_route_is_rejected():
    app = Veloce(openapi_url=None)

    @app.post(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
        mcp_description="Settings",
    )
    async def settings() -> dict:
        return {}

    with pytest.raises(ValueError, match="read-only"):
        _server(app)


def test_resource_without_uri_is_rejected():
    app = Veloce(openapi_url=None)

    @app.get("/settings", expose_as_mcp_resource=True, mcp_description="Settings")
    async def settings() -> dict:
        return {}

    with pytest.raises(ValueError, match="mcp_resource_uri"):
        _server(app)


def test_resource_uri_template_variable_mismatch_is_rejected():
    app = Veloce(openapi_url=None)

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://{wrong_name}",
        mcp_description="A user record",
    )
    async def user(user_id: int) -> dict:
        return {"id": user_id}

    with pytest.raises(ValueError, match="must match its path parameters"):
        _server(app)


def test_resource_missing_description_is_rejected():
    app = Veloce(openapi_url=None)

    @app.get(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
    )
    async def settings() -> dict:
        return {}

    with pytest.raises(ValueError, match="description"):
        _server(app)


def test_duplicate_resource_uri_is_rejected():
    app = Veloce(openapi_url=None)

    @app.get(
        "/a",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
        mcp_description="A",
    )
    async def a() -> dict:
        return {}

    @app.get(
        "/b",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
        mcp_description="B",
    )
    async def b() -> dict:
        return {}

    with pytest.raises(ValueError, match="Duplicate MCP resource URI"):
        _server(app)


def test_resource_read_response_model_filters_fields():
    """A resource route's `response_model` filters the body the agent reads, so a
    field outside the model never leaks over a resource read."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/me",
        response_model=PublicUser,
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://me",
        mcp_description="Current user",
    )
    async def me() -> dict:
        return {"id": 1, "name": "ada", "password": "s3cret"}

    out = _read_resource(app, "users://me")
    payload = orjson.loads(out["result"]["contents"][0]["text"])
    assert payload == {"id": 1, "name": "ada"}
    assert "password" not in payload


def test_resource_read_binary_returns_blob():
    app = Veloce(openapi_url=None)
    png = b"\x89PNG\r\n\x1a\n\x00\x00binary"

    @app.get(
        "/logo",
        expose_as_mcp_resource=True,
        mcp_resource_uri="assets://logo.png",
        mcp_description="The logo image",
    )
    async def logo() -> Response:
        return Response(body=png, content_type="image/png")

    out = _read_resource(app, "assets://logo.png")
    entry = out["result"]["contents"][0]
    assert entry["mimeType"] == "image/png"
    assert "text" not in entry
    assert base64.b64decode(entry["blob"]) == png


# -- Non-text tool content (image / audio) ----------------------------


def test_pure_tool_image_response_emits_image_block():
    app = Veloce(openapi_url=None)
    png = b"\x89PNG\r\n\x1a\nfake-image-bytes"

    @app.mcp_tool(description="Render a chart")
    async def chart() -> Response:
        return Response(body=png, content_type="image/png")

    result = _call(app, "chart", {})["result"]
    block = result["content"][0]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    assert base64.b64decode(block["data"]) == png
    # An image body has no text form, so no decoded-text block is emitted.
    assert len(result["content"]) == 1


def test_route_tool_audio_response_emits_audio_block():
    app = Veloce(openapi_url=None)
    wav = b"RIFF....WAVEfake-audio"

    @app.get("/say", expose_as_mcp_tool=True, mcp_description="Synthesize speech")
    async def say() -> Response:
        return Response(body=wav, content_type="audio/wav")

    result = _call(app, "say", {})["result"]
    block = result["content"][0]
    assert block["type"] == "audio"
    assert block["mimeType"] == "audio/wav"
    assert base64.b64decode(block["data"]) == wav
    assert "structuredContent" not in result


def test_non_binary_response_still_emits_text_block():
    """A JSON/text response is unaffected by the non-text shaping path."""
    app = Veloce(openapi_url=None)

    @app.get("/data2", expose_as_mcp_tool=True, mcp_description="Raw data")
    async def data2() -> JSONResponse:
        return JSONResponse({"value": 42})

    result = _call(app, "data2", {})["result"]
    assert result["content"][0]["type"] == "text"
    assert orjson.loads(result["content"][0]["text"]) == {"value": 42}


# -- Prompts ----------------------------------------------------------


def _list_prompts(app: Veloce) -> dict[str, dict]:
    """Drive one `prompts/list` and return the entries keyed by prompt name."""
    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "prompts/list", "params": {}})
    out = asyncio.run(pipe.run())[0]
    return {p["name"]: p for p in out["result"]["prompts"]}


def _get_prompt(app: Veloce, name: str, arguments: dict | None = None) -> dict:
    """Drive one `prompts/get` and return the single response object."""
    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "prompts/get",
            "params": {"name": name, "arguments": arguments or {}},
        }
    )
    return asyncio.run(pipe.run())[0]


def test_prompt_is_listed_with_arguments():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Summarise a topic")
    async def summarise(topic: str, style: str = "concise") -> str:
        return f"Summarise {topic} ({style})."

    listed = _list_prompts(app)
    assert "summarise" in listed
    entry = listed["summarise"]
    assert entry["description"] == "Summarise a topic"
    args = {a["name"]: a for a in entry["arguments"]}
    assert args["topic"]["required"] is True
    # A parameter with a default is an optional argument.
    assert args["style"]["required"] is False


def test_prompt_get_string_return_is_user_message():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Greeting")
    async def greet() -> str:
        return "Hello there."

    out = _get_prompt(app, "greet")
    assert "error" not in out
    result = out["result"]
    assert result["description"] == "Greeting"
    assert result["messages"] == [
        {"role": "user", "content": {"type": "text", "text": "Hello there."}}
    ]


def test_prompt_get_passes_arguments():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Summarise a topic")
    async def summarise(topic: str) -> str:
        return f"Summarise {topic} in three bullet points."

    result = _get_prompt(app, "summarise", {"topic": "veloce"})["result"]
    text = result["messages"][0]["content"]["text"]
    assert text == "Summarise veloce in three bullet points."


def test_prompt_get_message_list_is_normalised():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="A two-turn exchange")
    async def chat() -> list:
        return [
            {"role": "assistant", "content": "How can I help?"},
            {"role": "user", "content": {"type": "text", "text": "Explain MCP."}},
        ]

    messages = _get_prompt(app, "chat")["result"]["messages"]
    assert messages[0] == {
        "role": "assistant",
        "content": {"type": "text", "text": "How can I help?"},
    }
    assert messages[1] == {
        "role": "user",
        "content": {"type": "text", "text": "Explain MCP."},
    }


def test_prompt_unknown_name_is_invalid_params():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Greeting")
    async def greet() -> str:
        return "hi"

    out = _get_prompt(app, "nope")
    assert out["error"]["code"] == -32602


def test_prompt_missing_required_argument_is_invalid_params():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Summarise a topic")
    async def summarise(topic: str) -> str:
        return f"Summarise {topic}."

    out = _get_prompt(app, "summarise", {})
    assert out["error"]["code"] == -32602


def test_prompt_dependency_is_resolved():
    app = Veloce(openapi_url=None)

    def tone() -> str:
        return "friendly"

    @app.mcp_prompt(description="Greeting in a tone")
    async def greet(style: str = Depends(tone)) -> str:
        return f"Say hello in a {style} tone."

    # `style` is injected, so it is not advertised as a prompt argument.
    assert _list_prompts(app)["greet"].get("arguments", []) == []
    text = _get_prompt(app, "greet")["result"]["messages"][0]["content"]["text"]
    assert text == "Say hello in a friendly tone."


def test_prompt_context_is_injected():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Echo the prompt name")
    async def whoami(ctx: MCPContext) -> str:
        return ctx.tool_name

    # The context parameter is not advertised as an argument.
    assert _list_prompts(app)["whoami"].get("arguments", []) == []
    text = _get_prompt(app, "whoami")["result"]["messages"][0]["content"]["text"]
    assert text == "whoami"


def test_prompt_sync_handler_is_offloaded():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Sync greeting")
    def greet(name: str) -> str:
        return f"Hello, {name}."

    text = _get_prompt(app, "greet", {"name": "ada"})["result"]["messages"][0]["content"]["text"]
    assert text == "Hello, ada."


def test_prompt_namespace_prefixes_name():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Namespaced", namespace="docs")
    async def intro() -> str:
        return "Intro."

    assert "docs_intro" in _list_prompts(app)


def test_prompt_duplicate_name_raises():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="One")
    async def greet() -> str:
        return "one"

    app._mcp_prompts.append((greet, "greet", "Two", None, None))
    with pytest.raises(ValueError, match="Duplicate MCP prompt"):
        _server(app)


def test_prompt_missing_description_raises():
    app = Veloce(openapi_url=None)

    with pytest.raises(ValueError, match="description"):

        @app.mcp_prompt(description="")
        async def bad() -> str:
            return "x"


def test_initialize_advertises_prompts_when_present():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Greeting")
    async def greet() -> str:
        return "hi"

    caps = _initialize(app, {})["result"]["capabilities"]
    assert caps["prompts"] == {"listChanged": False}


def test_initialize_omits_prompts_capability_when_none():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    caps = _initialize(app, {})["result"]["capabilities"]
    assert "prompts" not in caps


# -- Progress / logging notifications ---------------------------------


def test_progress_notification_emitted_with_token():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work with progress")
    async def work(ctx: MCPContext) -> str:
        await ctx.report_progress(1, 2)
        await ctx.report_progress(2, 2)
        return "done"

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "work", "arguments": {}, "_meta": {"progressToken": "p1"}},
        }
    )
    out = asyncio.run(pipe.run())

    progresses = [m for m in out if m.get("method") == "notifications/progress"]
    assert len(progresses) == 2
    assert progresses[0]["params"] == {"progressToken": "p1", "progress": 1, "total": 2}
    # The result is written after the in-call progress notifications.
    result = next(m for m in out if m.get("id") == 1)
    assert result["result"]["content"][0]["text"] == "done"
    assert out[-1] is result


def test_progress_is_noop_without_token():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work with progress")
    async def work(ctx: MCPContext) -> str:
        await ctx.report_progress(1, 2)
        return "done"

    # No `_meta.progressToken`, so the client did not opt into progress.
    out = asyncio.run(_drive_call(app, "work"))
    assert [m for m in out if m.get("method") == "notifications/progress"] == []


def test_log_notification_emitted():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Log then return")
    async def work(ctx: MCPContext) -> str:
        await ctx.log("info", "working")
        return "ok"

    out = asyncio.run(_drive_call(app, "work"))
    messages = [m for m in out if m.get("method") == "notifications/message"]
    assert len(messages) == 1
    assert messages[0]["params"]["level"] == "info"
    assert messages[0]["params"]["data"] == "working"


def test_log_filtered_below_set_level():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Log then return")
    async def work(ctx: MCPContext) -> str:
        await ctx.log("info", "noisy")
        return "ok"

    pipe = _Pipe(_server(app))
    # Raise the minimum to `error`, then call: the `info` log is below it.
    pipe.feed(
        {"jsonrpc": "2.0", "id": 1, "method": "logging/setLevel", "params": {"level": "error"}}
    )
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "work", "arguments": {}},
        }
    )
    out = asyncio.run(pipe.run())

    assert next(m for m in out if m.get("id") == 1)["result"] == {}
    assert [m for m in out if m.get("method") == "notifications/message"] == []


def test_logging_set_level_rejects_invalid_level():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    pipe = _Pipe(_server(app))
    pipe.feed(
        {"jsonrpc": "2.0", "id": 1, "method": "logging/setLevel", "params": {"level": "verbose"}}
    )
    out = asyncio.run(pipe.run())
    assert out[0]["error"]["code"] == -32602


def test_logging_capability_advertised():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    caps = _initialize(app, {})["result"]["capabilities"]
    assert caps["logging"] == {}


def _drive_call(app: Veloce, name: str, arguments: dict | None = None):
    """Drive one `tools/call` through the transport and return every written line."""
    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
    )
    return pipe.run()


# -- Per-call timeout (MCP_CALL_TIMEOUT) ------------------------------


def test_tool_call_timeout_is_in_band_error():
    app = Veloce(openapi_url=None)
    app.config["MCP_CALL_TIMEOUT"] = 0.05

    @app.mcp_tool(description="Hangs forever")
    async def hang() -> str:
        await asyncio.sleep(10)
        return "never"

    result = _call(app, "hang", {})["result"]
    assert result["isError"] is True
    assert "timeout" in result["content"][0]["text"].lower()


def test_resource_read_timeout_is_error():
    app = Veloce(openapi_url=None)
    app.config["MCP_CALL_TIMEOUT"] = 0.05

    @app.get(
        "/slow",
        expose_as_mcp_resource=True,
        mcp_resource_uri="slow://data",
        mcp_description="Slow resource",
    )
    async def slow() -> dict:
        await asyncio.sleep(10)
        return {}

    out = _read_resource(app, "slow://data")
    assert out["error"]["code"] == -32603


def test_no_timeout_by_default_completes():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Brief await")
    async def brief() -> str:
        await asyncio.sleep(0.01)
        return "ok"

    # With no MCP_CALL_TIMEOUT configured, the call runs unbounded and completes.
    result = _call(app, "brief", {})["result"]
    assert result["content"][0]["text"] == "ok"


# -- Error-text gating (debug) ----------------------------------------


def test_pure_tool_error_text_is_generic_without_debug():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Leaks a secret in its error")
    async def boom() -> str:
        raise RuntimeError("postgres://user:hunter2@db/secret")

    result = _call(app, "boom", {})["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    # The raw exception text (carrying a credential) is not surfaced.
    assert "hunter2" not in text
    assert text == "the tool raised an internal error"


def test_pure_tool_error_text_shown_with_debug():
    app = Veloce(openapi_url=None, debug=True)

    @app.mcp_tool(description="Surfaces its error in debug")
    async def boom() -> str:
        raise RuntimeError("a helpful development message")

    result = _call(app, "boom", {})["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "a helpful development message"


# -- Streamable HTTP transport ----------------------------------------


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


def test_http_tool_call_returns_json():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    resp = app.test_client().post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
        },
    )
    assert resp.status_code == 200
    body = orjson.loads(resp.body)
    assert body["result"]["content"][0]["text"] == "5"


def test_http_initialize_returns_capabilities():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", path="/rpc")
    resp = app.test_client().post(
        "/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    body = orjson.loads(resp.body)
    assert body["result"]["serverInfo"]["name"]
    assert "tools" in body["result"]["capabilities"]


def test_http_notification_returns_202():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    # A notification (no id) carries no reply.
    resp = app.test_client().post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert resp.status_code == 202
    assert resp.body == b""


def test_http_parse_error_is_400():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    resp = app.test_client().post(
        "/mcp", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 400
    assert orjson.loads(resp.body)["error"]["code"] == -32700


def test_http_unknown_method_is_json_error():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    resp = app.test_client().post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "does/not/exist", "params": {}}
    )
    body = orjson.loads(resp.body)
    assert body["error"]["code"] == -32601


def test_http_resource_read_over_http():
    app = Veloce(openapi_url=None)

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://{user_id}",
        mcp_description="A user record",
    )
    async def user(user_id: int) -> dict:
        return {"id": user_id}

    app.mount_mcp(transport="http")
    resp = app.test_client().post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "users://9"},
        },
    )
    body = orjson.loads(resp.body)
    assert orjson.loads(body["result"]["contents"][0]["text"]) == {"id": 9}


def test_http_sse_streams_progress_then_response():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work with progress")
    async def work(ctx: MCPContext) -> str:
        await ctx.report_progress(1, 2)
        await ctx.report_progress(2, 2)
        return "done"

    app.mount_mcp(transport="http")
    resp = app.test_client().post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "work", "arguments": {}, "_meta": {"progressToken": "p1"}},
        },
        headers={"accept": "text/event-stream"},
    )
    assert "text/event-stream" in resp.content_type
    events = _parse_sse(resp.body)
    progresses = [e for e in events if e.get("method") == "notifications/progress"]
    assert len(progresses) == 2
    assert progresses[0]["params"]["progressToken"] == "p1"
    # The final SSE event is the JSON-RPC response, after the progress events.
    response = events[-1]
    assert response["id"] == 1
    assert response["result"]["content"][0]["text"] == "done"


def test_http_sse_concurrent_calls_do_not_cross_notifications():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Echo a label as progress")
    async def echo(label: str, ctx: MCPContext) -> str:
        await ctx.report_progress(1, 1, message=label)
        return label

    app.mount_mcp(transport="http")
    client = app.test_client()

    def call(label: str) -> list[dict]:
        resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "echo",
                    "arguments": {"label": label},
                    "_meta": {"progressToken": label},
                },
            },
            headers={"accept": "text/event-stream"},
        )
        return _parse_sse(resp.body)

    # Each call's progress notification carries only its own token, never the
    # other call's - the per-request notifier scoping holds.
    a_events = call("a")
    b_events = call("b")
    a_tokens = {e["params"]["progressToken"] for e in a_events if e.get("method")}
    b_tokens = {e["params"]["progressToken"] for e in b_events if e.get("method")}
    assert a_tokens == {"a"}
    assert b_tokens == {"b"}


# -- Principal + per-tool scopes --------------------------------------


@pytest.fixture(autouse=True)
def _reset_principal():
    """Keep the principal contextvar from leaking between tests."""
    set_principal(None)
    yield
    set_principal(None)


def test_principal_has_scopes():
    p = Principal(subject="u1", scopes=frozenset({"a", "b"}))
    assert p.has_scope("a")
    assert p.has_scopes(["a", "b"])
    assert not p.has_scopes(["a", "c"])


def test_scoped_tool_rejected_without_principal():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Privileged", scopes=["admin"])
    async def wipe() -> str:
        return "wiped"

    # No principal set (unauthenticated): a scoped tool cannot be satisfied.
    out = _call(app, "wipe", {})
    assert out["error"]["code"] == -32003
    assert "insufficient_scope" in out["error"]["message"]
    assert out["error"]["data"]["requiredScopes"] == ["admin"]


def test_scoped_tool_rejected_with_insufficient_scope():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Privileged", scopes=["admin"])
    async def wipe() -> str:
        return "wiped"

    set_principal(Principal(subject="u1", scopes=frozenset({"read"})))
    out = _call(app, "wipe", {})
    assert out["error"]["code"] == -32003
    assert "insufficient_scope" in out["error"]["message"]


def test_scoped_tool_allowed_with_scope():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Privileged", scopes=["admin"])
    async def wipe() -> str:
        return "wiped"

    set_principal(Principal(subject="u1", scopes=frozenset({"admin", "read"})))
    result = _call(app, "wipe", {})["result"]
    assert result.get("isError") is not True
    assert result["content"][0]["text"] == "wiped"


def test_scoped_resource_forbidden_without_scope():
    app = Veloce(openapi_url=None)

    @app.get(
        "/secret",
        expose_as_mcp_resource=True,
        mcp_resource_uri="secret://data",
        mcp_description="Secret data",
        mcp_scopes=["secrets:read"],
    )
    async def secret() -> dict:
        return {"value": 1}

    set_principal(Principal(scopes=frozenset({"other"})))
    out = _read_resource(app, "secret://data")
    assert out["error"]["code"] == -32003
    assert "insufficient_scope" in out["error"]["message"]


def test_scoped_prompt_forbidden_without_scope():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Privileged prompt", scopes=["prompts:use"])
    async def secret() -> str:
        return "secret"

    set_principal(Principal(scopes=frozenset()))
    out = _get_prompt(app, "secret")
    assert out["error"]["code"] == -32003


def test_tool_reads_current_principal():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Echo the caller subject")
    async def whoami() -> str:
        p = current_principal()
        return p.subject if p else "anon"

    set_principal(Principal(subject="alice"))
    assert _call(app, "whoami", {})["result"]["content"][0]["text"] == "alice"


def test_request_is_mcp_true_over_mcp():
    app = Veloce(openapi_url=None)

    from veloce import Request

    @app.get("/probe", expose_as_mcp_tool=True, mcp_description="Probe")
    async def probe(request: Request) -> dict:
        return {"is_mcp": request.is_mcp}

    # Over MCP the replayed request is flagged.
    out = _call(app, "probe", {})
    assert orjson.loads(out["result"]["content"][0]["text"]) == {"is_mcp": True}
    # Over HTTP it is a real request, not an MCP replay.
    http = app.test_client().get("/probe")
    assert http.json()["is_mcp"] is False


# -- HTTP transport authentication (OAuth Resource Server) ------------


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


def test_http_auth_missing_token_is_401():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", auth=_auth())
    resp = app.test_client().post("/mcp", json=_mcp_call_body("add", {"a": 1, "b": 2}))
    assert resp.status_code == 401
    assert "Bearer" in resp.headers.get("www-authenticate", "")
    assert "oauth-protected-resource" in resp.headers.get("www-authenticate", "")


def test_http_auth_invalid_token_is_401():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", auth=_auth())
    resp = app.test_client().post(
        "/mcp",
        json=_mcp_call_body("add", {"a": 1, "b": 2}),
        headers={"authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_http_auth_valid_token_dispatches():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", auth=_auth())
    resp = app.test_client().post(
        "/mcp",
        json=_mcp_call_body("add", {"a": 2, "b": 3}),
        headers={"authorization": "Bearer good"},
    )
    assert resp.status_code == 200
    assert orjson.loads(resp.body)["result"]["content"][0]["text"] == "5"


def test_http_auth_endpoint_scope_is_403():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    # Endpoint requires mcp:tools; the 'noscope' principal lacks it.
    app.mount_mcp(transport="http", auth=_auth(required_scopes=["mcp:tools"]))
    resp = app.test_client().post(
        "/mcp",
        json=_mcp_call_body("add", {"a": 1, "b": 2}),
        headers={"authorization": "Bearer noscope"},
    )
    assert resp.status_code == 403
    assert "insufficient_scope" in resp.headers.get("www-authenticate", "")


def test_http_auth_principal_visible_to_tool():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Echo subject")
    async def whoami() -> str:
        p = current_principal()
        return p.subject if p else "anon"

    app.mount_mcp(transport="http", auth=_auth())
    resp = app.test_client().post(
        "/mcp", json=_mcp_call_body("whoami"), headers={"authorization": "Bearer good"}
    )
    assert orjson.loads(resp.body)["result"]["content"][0]["text"] == "agent-1"


def test_http_auth_per_tool_scope_uses_token_scopes():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Privileged", scopes=["admin"])
    async def wipe() -> str:
        return "wiped"

    # Token grants mcp:tools but not admin, so the per-tool scope check rejects
    # with an HTTP 403 + insufficient_scope challenge.
    app.mount_mcp(transport="http", auth=_auth())
    resp = app.test_client().post(
        "/mcp", json=_mcp_call_body("wipe"), headers={"authorization": "Bearer good"}
    )
    assert resp.status_code == 403
    assert "insufficient_scope" in resp.headers.get("www-authenticate", "")
    assert orjson.loads(resp.body)["error"]["code"] == -32003


def test_http_protected_resource_metadata_served():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(
        transport="http",
        auth=MCPAuth(
            verify=_verify,
            resource_server_url="https://api.example.com/mcp",
            authorization_servers=["https://auth.example.com"],
            scopes_supported=["mcp:tools"],
        ),
    )
    resp = app.test_client().get("/.well-known/oauth-protected-resource")
    doc = orjson.loads(resp.body)
    assert doc["resource"] == "https://api.example.com/mcp"
    assert doc["authorization_servers"] == ["https://auth.example.com"]
    assert doc["scopes_supported"] == ["mcp:tools"]


def test_http_auth_async_verifier():
    app = Veloce(openapi_url=None)

    async def averify(token: str):
        return Principal(subject="async-agent") if token == "ok" else None

    @app.mcp_tool(description="Echo subject")
    async def whoami() -> str:
        p = current_principal()
        return p.subject if p else "anon"

    app.mount_mcp(transport="http", auth=_auth(verify=averify))
    resp = app.test_client().post(
        "/mcp", json=_mcp_call_body("whoami"), headers={"authorization": "Bearer ok"}
    )
    assert orjson.loads(resp.body)["result"]["content"][0]["text"] == "async-agent"


# -- Hardening: single is_mcp check, Origin, metadata, provenance -----


def test_exclude_middleware_and_is_mcp_cover_transport_and_replay():
    app = Veloce(openapi_url=None)

    from veloce import Middleware

    class Auth(Middleware):
        async def process_request(self, request):
            if request.is_mcp:  # replayed tool call → transport already authed
                return None
            return JSONResponse({"detail": "no"}, status_code=401)

    app.add_middleware(Auth)

    @app.get("/order/{n}", expose_as_mcp_tool=True, mcp_description="Get order")
    async def order(n: int) -> dict:
        return {"n": n}

    # exclude_middleware drops Auth from the /mcp transport route (it has its own
    # auth); `if request.is_mcp` drops it from the replayed tool call. No paths.
    app.mount_mcp(transport="http", auth=_auth(), exclude_middleware=["Auth"])
    resp = app.test_client().post(
        "/mcp", json=_mcp_call_body("order", {"n": 7}), headers={"authorization": "Bearer good"}
    )
    assert resp.status_code == 200
    assert orjson.loads(orjson.loads(resp.body)["result"]["content"][0]["text"]) == {"n": 7}


def test_origin_validation():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", allowed_origins=["https://app.example.com"])
    client = app.test_client()
    body = _mcp_call_body("add", {"a": 1, "b": 1})

    # A browser Origin outside the allowlist is rejected (DNS-rebinding defense).
    bad = client.post("/mcp", json=body, headers={"origin": "https://evil.example"})
    assert bad.status_code == 403
    # An allowed Origin passes.
    ok = client.post("/mcp", json=body, headers={"origin": "https://app.example.com"})
    assert ok.status_code == 200
    # No Origin header (a non-browser client) passes.
    none = client.post("/mcp", json=body)
    assert none.status_code == 200


def test_mcpauth_requires_metadata():
    with pytest.raises(ValueError, match="resource_server_url"):
        MCPAuth(verify=_verify, authorization_servers=["https://auth.example.com"])
    with pytest.raises(ValueError, match="authorization_servers"):
        MCPAuth(verify=_verify, resource_server_url="https://api.example.com/mcp")


def test_principal_token_not_in_repr():
    p = Principal(subject="u", token="super-secret-token-value")
    assert "super-secret-token-value" not in repr(p)


def test_tool_argument_cannot_spoof_header_or_cookie():
    app = Veloce(openapi_url=None)

    from veloce import Request

    @app.get(
        "/probe", expose_as_mcp_resource=False, mcp_description="Probe", expose_as_mcp_tool=True
    )
    async def probe(request: Request) -> dict:
        # A Security scheme reading a header / cookie (HTTPBearer, APIKeyHeader,
        # APIKeyCookie) must NOT see an agent-supplied value: tool arguments are
        # never seeded into the synthetic request's headers or cookies.
        return {
            "auth": request.headers.get("authorization"),
            "api_key": request.headers.get("x-api-key"),
            "cookie": request.cookies.get("session"),
        }

    out = _call(
        app,
        "probe",
        {"authorization": "Bearer spoofed", "x_api_key": "forged", "session": "hijacked"},
    )
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"auth": None, "api_key": None, "cookie": None}


def test_origin_empty_allowlist_rejects_all_browsers():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    # An explicit empty allowlist denies every browser Origin (it does not
    # silently disable the check).
    app.mount_mcp(transport="http", allowed_origins=[])
    body = _mcp_call_body("add", {"a": 1, "b": 1})
    blocked = app.test_client().post("/mcp", json=body, headers={"origin": "https://any.example"})
    assert blocked.status_code == 403
    # A non-browser client (no Origin) is still allowed.
    assert app.test_client().post("/mcp", json=body).status_code == 200


def test_get_on_mcp_endpoint_returns_405():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    # The endpoint has no standalone server-to-client stream, so a GET is the
    # spec-conformant 405 (not the app's generic method-not-allowed), with Allow.
    resp = app.test_client().get("/mcp")
    assert resp.status_code == 405
    assert resp.headers.get("allow") == "POST"


def test_unsupported_protocol_version_header_is_400():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    client = app.test_client()
    body = _mcp_call_body("add", {"a": 1, "b": 1})

    # A present, unsupported version is rejected with a JSON-RPC error at 400.
    bad = client.post("/mcp", json=body, headers={"mcp-protocol-version": "1999-01-01"})
    assert bad.status_code == 400
    assert orjson.loads(bad.body)["error"]["code"] == -32600
    # A supported version passes.
    ok = client.post("/mcp", json=body, headers={"mcp-protocol-version": "2025-06-18"})
    assert ok.status_code == 200


def test_missing_protocol_version_header_keeps_current_behavior():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    # No MCP-Protocol-Version header: the legacy path is unchanged (no 400).
    resp = app.test_client().post("/mcp", json=_mcp_call_body("add", {"a": 1, "b": 1}))
    assert resp.status_code == 200


def test_sse_stream_opens_with_priming_event_and_closes_with_retry():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work")
    async def work() -> str:
        return "done"

    app.mount_mcp(transport="http")
    resp = app.test_client().post(
        "/mcp", json=_mcp_call_body("work"), headers={"accept": "text/event-stream"}
    )
    frames = resp.body.split(b"\n\n")
    # First frame: a priming event carrying an id and an empty data field.
    assert frames[0].startswith(b"id: ")
    assert b"data: " in frames[0]
    # Last non-empty frame: a retry field hinting the reconnect delay.
    closing = [f for f in frames if f.strip()][-1]
    assert closing.strip() == b"retry: 3000"
    # The JSON-RPC response is still delivered between the two.
    events = _parse_sse(resp.body)
    assert events[-1]["result"]["content"][0]["text"] == "done"


async def test_sse_disconnection_does_not_cancel_the_call():
    from veloce.contrib.mcp.server import MCPServer
    from veloce.contrib.mcp.transports.http import _stream_response

    app = Veloce(openapi_url=None)
    started = asyncio.Event()
    release = asyncio.Event()
    ran_to_completion = asyncio.Event()

    @app.mcp_tool(description="Work whose completion is observable after disconnect")
    async def work(ctx: MCPContext) -> str:
        started.set()
        # Block until the test releases the call, keeping it in flight across the
        # consumer's disconnect so cancellation (if any) would land here.
        await release.wait()
        ran_to_completion.set()
        return "done"

    stream = _stream_response(MCPServer(app), _mcp_call_body("work"), None)
    gen = stream._stream
    # Consume only the priming frame, then disconnect by closing the generator -
    # simulating a client dropping the SSE stream while the call is in flight.
    await gen.__anext__()
    await started.wait()
    await gen.aclose()
    # The call is still blocked (in flight) at disconnect time; release it now.
    release.set()

    # The dispatched call is left running to completion; a dropped connection is
    # not treated as the client cancelling its request (MCP transport rule).
    await asyncio.wait_for(ran_to_completion.wait(), timeout=1)
    assert ran_to_completion.is_set()


def test_http_log_level_does_not_bleed_across_requests():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Log then return")
    async def work(ctx: MCPContext) -> str:
        await ctx.log("info", "hello")
        return "ok"

    app.mount_mcp(transport="http")
    client = app.test_client()
    # One HTTP client raises the log floor to `error`...
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logging/setLevel",
            "params": {"level": "error"},
        },
    )
    # ...which must NOT carry into a separate request (the level is per-request,
    # not shared on the one MCPServer the HTTP transport reuses).
    resp = client.post("/mcp", json=_mcp_call_body("work"), headers={"accept": "text/event-stream"})
    messages = [e for e in _parse_sse(resp.body) if e.get("method") == "notifications/message"]
    assert len(messages) == 1  # the info log is emitted; the other request's setLevel did not bleed
