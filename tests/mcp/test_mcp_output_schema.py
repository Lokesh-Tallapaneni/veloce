"""Output schemas and the structured content a call returns.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import asyncio
import time

import orjson

from tests._mcp_shared import (
    AliasedOut,
    AnnotatedOut,
    ComputedOut,
    Customer,
    FullUser,
    Node,
    PublicUser,
    _call,
    _initialize,
    _list_tools,
)
from veloce import (
    EventSourceResponse,
    HTTPException,
    JSONResponse,
    PlainTextResponse,
    ServerSentEvent,
    StreamingResponse,
    Veloce,
)
from veloce.contrib.mcp import _tasks as mcp_tasks
from veloce.contrib.mcp.server import LATEST_PROTOCOL_VERSION

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

    monkeypatch.setattr(mcp_tasks, "_STREAM_DRAIN_TIMEOUT", 0.05)

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

    monkeypatch.setattr(mcp_tasks, "_STREAM_BUFFER_LIMIT", 8)

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

    monkeypatch.setattr(mcp_tasks, "_STREAM_DRAIN_TIMEOUT", 0.05)

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
