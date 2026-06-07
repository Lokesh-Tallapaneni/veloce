# MCP (Model Context Protocol)

Veloce can expose your handlers as [Model Context Protocol](https://modelcontextprotocol.io)
tools, so an AI agent can call them over JSON-RPC 2.0. Every Veloce route can
also be a tool an agent invokes.

The integration lives in `veloce.contrib.mcp`. It supports **tools** over the
**stdio** transport (the framing an MCP client uses when it launches your server
as a subprocess), negotiates the protocol version with the client, and exposes
tool metadata (annotations, title, output schema). Resources, prompts, and the
Streamable HTTP transport are planned for a later release.

## Registering an MCP-only tool

Use `@app.mcp_tool(...)` to register a tool that exists only for agents. The
input JSON Schema is derived from the function signature, and `Depends()`
parameters resolve through the same dependency machinery your routes use.

```python
from veloce import Veloce

app = Veloce()


@app.mcp_tool(description="Add two integers")
async def add(a: int, b: int) -> int:
    return a + b
```

`description` is required: it is the text the language model reads to decide
when to call the tool, and it is kept separate from the docstring.

Sync functions work too - they are offloaded to the thread pool, exactly like
sync route handlers, so they never block the event loop.

## Exposing an existing route

Pass `expose_as_mcp_tool=True` and a non-empty `mcp_description` on any route
to expose it as a tool as well as an HTTP endpoint.

```python
@app.get("/users/{user_id}", expose_as_mcp_tool=True, mcp_description="Fetch a user by id")
async def get_user(user_id: int):
    return {"id": user_id}
```

### Safety

Exposure is **default-closed**: no route is ever turned into a tool
automatically. A route — of any HTTP verb, including a mutating `POST` / `PUT` /
`DELETE` / `PATCH` — becomes agent-callable only when its author opts in
explicitly with `expose_as_mcp_tool=True`:

```python
@app.post("/users", expose_as_mcp_tool=True, mcp_description="Create a user")
async def create_user(user: User):
    ...
```

An exposed route keeps every guard it has as an HTTP endpoint — its `Security`
schemes, `Depends`, and middleware all run on the agent-facing call too, so
exposing a route never bypasses its authorization.

Every exposed handler must carry a non-empty `mcp_description`. A missing
description raises at registration time, before the server starts.

An exposed route is invoked inside the same request context an HTTP request
runs in: `current_app`, `g`, and the `request` proxy are bound, and the app's
request middleware (`Middleware.process_request`) and `@app.before_request`
hooks run before the handler, in the same order they run on the HTTP path. A
middleware or hook that returns a response (an auth `401`, for example)
short-circuits the call - the handler is not invoked, `teardown_request` still
runs, and the response becomes the tool result, surfaced as an error when its
status is `4xx`/`5xx`.

The synthetic `request` carries the wrapped route's real HTTP method and rule
path, so a handler, dependency, or hook that branches on `request.method` /
`request.path` sees the route's own values (the concrete path-parameter values
remain on `request.path_params`). A client-supplied parameter declared inside a
`Depends` dependency - including a body model - is advertised in the tool's
input schema, so `tools/list` and `tools/call` agree on the accepted inputs. A
route's rule `defaults=` fill any handler argument the call does not supply.

### Streaming responses

A route returning a `StreamingResponse` or an `EventSourceResponse` (SSE) has no
single body, but a `tools/call` returns one result, so the stream is drained and
joined into the tool result: a streamed JSON body is decoded back to a value, and
an SSE stream is returned as its event-framed text. Draining is bounded by both
a size cap (a result over 5 MiB) and a drain timeout (a stream that does not
complete in time); either bound returns an in-band error rather than buffering
without limit or blocking the serial stdio loop. Do not expose an unbounded /
infinite stream (a keep-alive SSE feed) as a tool - it will hit the timeout.

## Tool metadata

A tool exposed from a route carries metadata derived from the route, so an MCP
client can present it and reason about its effects without calling it:

- **`title`** - the route's `summary`, shown as the tool's human-readable name.
- **`annotations`** - advisory hints derived from the HTTP method: `readOnlyHint`
  (true for `GET`/`HEAD`), `idempotentHint` (true for `GET`/`HEAD`/`PUT`/`DELETE`),
  and `destructiveHint` (true for `PUT`/`PATCH`/`DELETE`, false for the additive
  `POST`). A client may surface a consent prompt for a destructive tool. A pure
  `@app.mcp_tool` has no HTTP method, so it carries no annotations.
- **`outputSchema`** - when the route declares a `response_model` (or the handler
  returns a Pydantic model), the tool advertises a standalone JSON Schema for its
  result. `tools/call` then returns the result as `structuredContent` alongside
  the text block, so a client receives the typed value the schema describes. A
  scalar or list result has no object schema and returns only the text block.

```python
from pydantic import BaseModel


class User(BaseModel):
    id: int
    name: str


@app.get(
    "/users/{user_id}",
    response_model=User,
    summary="Fetch a user",
    expose_as_mcp_tool=True,
    mcp_description="Look up a user by id",
)
async def get_user(user_id: int) -> User:
    return User(id=user_id, name="Ada")
# tools/list -> title "Fetch a user", readOnlyHint=true, outputSchema for User
# tools/call -> structuredContent {"id": ..., "name": ...} plus the text block
```

## The MCP context

A tool handler (or one of its dependencies) may declare a parameter typed
`MCPContext` to receive the per-call context; it is matched by that type
annotation, not by the parameter's name. It
carries the calling tool name and the raw argument mapping, plus inert
placeholders for the cancellation / progress / logging channels that later
protocol versions define.

```python
from veloce import MCPContext


@app.mcp_tool(description="Echo the calling tool name")
async def whoami(ctx: MCPContext) -> str:
    return ctx.tool_name
```

The context parameter is not part of the tool's input schema - the agent never
supplies it.

## Dependency injection

`Depends()` works in a tool exactly as it does in a route. Injected parameters
are resolved per call and never appear in the tool's input schema. `yield`-style
dependencies get their teardown run after the handler returns.

```python
from veloce import Depends


def get_db():
    db = connect()
    try:
        yield db
    finally:
        db.close()


@app.mcp_tool(description="Count rows in a table")
async def count(table: str, db=Depends(get_db)) -> int:
    return db.count(table)
```

## Blueprint namespacing

A tool exposed from a blueprint route is namespaced by the blueprint name. A
route named `billing.status` becomes the tool `billing_status`. An explicit
`@app.mcp_tool` can be namespaced with the `namespace=` argument:

```python
@app.mcp_tool(description="Add", namespace="math")
async def add(a: int, b: int) -> int:
    return a + b
# Tool name: "math_add"
```

## Serving over stdio

`app.mount_mcp(transport="stdio")` builds the tool registry (from
`@app.mcp_tool` registrations plus every route flagged `expose_as_mcp_tool=True`)
and returns a coroutine that serves JSON-RPC 2.0 on stdin/stdout until the
input closes.

```python
import asyncio

if __name__ == "__main__":
    asyncio.run(app.mount_mcp(transport="stdio"))
```

Point your MCP client at this script as a subprocess command; it will receive
`initialize` (negotiating the protocol version with the client), `ping`,
`tools/list`, and `tools/call` and respond on stdout.

The serve loop runs inside the app's lifespan, so every `@app.on_startup`
handler (database pools, `app.state`, caches) and the lifespan context manager
run before the first tool is served, and the matching shutdown runs after the
input closes - exactly as when the app is served by an ASGI server.

## Instrumentation

Each tool call fires the same `app.add_instrumentation` hooks an HTTP request
does. The `RequestMetrics` record carries `method="tools/call"`, `route` and
`path` set to the tool name, the call duration, and the call's real
`status_code` - the shaped response's status for a route-backed or
short-circuited call, `500` for an unhandled handler error, `200` only on
genuine success - so a metrics exporter can record tool usage and error rates
with no extra wiring.
```
