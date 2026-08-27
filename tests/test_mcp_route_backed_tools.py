"""A tool backed by an HTTP route behaves like that route.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import orjson

from tests._mcp_shared import (
    FullUser,
    Item,
    PublicUser,
    _call,
)
from veloce import (
    BackgroundTasks,
    Depends,
    HTTPException,
    JSONResponse,
    Response,
    Veloce,
)
from veloce.contrib.mcp import plan_bridge
from veloce.contrib.mcp.registry import build_registry
from veloce.dependency import DependencyResolver

# -- Response shaping for exposed routes ------------------------------


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
    # Identical to what the HTTP door emits for the same exception; the two
    # error builders drifting apart is the failure this pins against.
    assert payload == {"detail": "nope", "status_code": 404}


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


def test_sub_dependency_query_marker_coercion_failure_is_an_in_band_tool_error():
    """A coercion failure resolving a sub-dependency marker is reported in band,
    the same way a top-level marker's is.
    An argument-binding failure is a **tool execution** error reported in band
    (`result.isError`), not a JSON-RPC transport error. The spec reserves the
    error channel for an unknown tool, a malformed request or a server fault,
    and clients feed only execution errors back to the model - reporting a bad
    argument there would deny the model the one signal it can act on.

    Named for that. It used to be `..._is_invalid_params`, with a docstring and
    a leading comment both asserting the opposite of the assertion below; a
    later round added a rebuttal comment above the assertion rather than
    correcting the name and the prose, so the test read as self-contradictory.
    """
    from veloce import Query

    app = Veloce(openapi_url=None)

    def lookup(count: int = Query(...)) -> int:
        return count

    @app.get("/cnt", expose_as_mcp_tool=True, mcp_description="Count")
    async def cnt(n: int = Depends(lookup)) -> dict:
        return {"n": n}

    out = _call(app, "cnt", {"count": "not-an-int"})
    # Input validation is a *tool execution* error, not a protocol error: the
    # spec reserves the JSON-RPC channel for an unknown tool, a malformed
    # request, or a server fault, and clients feed only execution errors back
    # to the model. Reporting a bad argument on the error channel would deny
    # the model the one signal it can act on.
    assert out["result"]["isError"] is True
    assert "Invalid arguments" in out["result"]["content"][0]["text"]


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


def test_the_mcp_bridge_delegates_the_injected_slots_to_the_resolver():
    """`K_RESPONSE` / `K_BG_TASKS` bind through `DependencyResolver`, not through
    a second copy of its body. A local re-implementation here would drift the
    moment the HTTP side changed its sentinel or its state key, and the HTTP
    test suite would not see it."""
    assert plan_bridge._injected_response is DependencyResolver._bind_injected_response
    assert plan_bridge._background_tasks is DependencyResolver._bind_background_tasks


def test_the_mcp_injected_response_starts_at_the_never_set_sentinel():
    """`status_code = 0` is the "handler never set it" marker `_build_response`
    tests before merging. It must reach an MCP handler that way, and must never
    itself be merged onto the result."""
    app = Veloce(openapi_url=None)
    seen: list[int] = []

    @app.get("/probe", expose_as_mcp_tool=True, mcp_description="Probe")
    async def probe(response: Response) -> dict:
        seen.append(response.status_code)
        return {"ok": True}

    out = _call(app, "probe", {})
    assert "error" not in out
    assert seen == [0]
    assert out["result"].get("isError") is not True


def test_an_mcp_dependency_and_handler_share_one_injected_response():
    """Same contract as the HTTP path: one Response per call, not one per slot."""
    app = Veloce(openapi_url=None)
    seen: list[Response] = []

    def stamp(response: Response) -> None:
        seen.append(response)

    @app.get("/shared", expose_as_mcp_tool=True, mcp_description="Shared")
    async def shared(response: Response, _: None = Depends(stamp)) -> dict:
        seen.append(response)
        return {"ok": True}

    out = _call(app, "shared", {})
    assert "error" not in out
    assert len(seen) == 2
    assert seen[0] is seen[1]


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
    # The model validates against the whole argument mapping, so its fields are
    # the tool's inputs. Declaring the parameter name instead would publish a
    # shape the call path rejects.
    assert set(schema["properties"]) == {"name", "qty"}
    assert "item" not in schema["properties"]
    # The published contract has to be the one a caller can actually satisfy.
    out = _call(app, "mk2", {"name": "widget", "qty": 3})
    assert "error" not in out
    assert not out["result"].get("isError"), out["result"]["content"][0]["text"]


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
