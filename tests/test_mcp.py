"""MCP integration tests - registry, schema, stdio round-trip, DI, safety."""

from __future__ import annotations

import asyncio

import orjson
import pytest
from pydantic import BaseModel

from veloce import (
    BackgroundTasks,
    Blueprint,
    Depends,
    HTTPException,
    JSONResponse,
    MCPContext,
    Response,
    SecurityScopes,
    Veloce,
)
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
        app.mount_mcp(transport="http")


def test_duplicate_tool_name_raises():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="One")
    async def dup():
        return 1

    # A second tool resolving to the same name collides at registry build.
    app._mcp_tools.append((dup, "dup", "Two", None))
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


def test_exposed_route_returning_streaming_response_yields_iserror():
    """A route returning a StreamingResponse (no buffered body) is rejected with
    a clear isError result, not an empty output (v1 limitation)."""
    from veloce import StreamingResponse

    app = Veloce(openapi_url=None)

    @app.get("/stream", expose_as_mcp_tool=True, mcp_description="Stream chunks")
    async def stream() -> StreamingResponse:
        async def gen():
            yield b"a"
            yield b"b"

        return StreamingResponse(gen())

    out = _call(app, "stream", {})
    assert "error" not in out  # in-band tool error, not a transport error
    assert out["result"]["isError"] is True
    text = out["result"]["content"][0]["text"]
    assert "streaming" in text.lower()
    assert text != ""


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
