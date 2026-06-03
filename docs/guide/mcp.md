# MCP (Model Context Protocol)

Veloce can expose your handlers as [Model Context Protocol](https://modelcontextprotocol.io)
tools, so an AI agent can call them over JSON-RPC 2.0. Every Veloce route can
also be a tool an agent invokes.

The integration lives in `veloce.contrib.mcp`. Version 1 supports **tools**
over the **stdio** transport (the framing an MCP client uses when it launches
your server as a subprocess). Resources, prompts, and the HTTP/SSE transport
are planned for a later release.

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

Routes bound to a mutating verb (`POST`, `PUT`, `DELETE`, `PATCH`) are **never**
auto-exposed. To make one callable by an agent you must opt in explicitly:

```python
@app.post("/users", expose_as_mcp_tool=True, mcp_description="Create a user")
async def create_user(user: User):
    ...
```

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

### Streaming responses (v1 limitation)

A route returning a `StreamingResponse` or server-sent events has no buffered
body, so v1 cannot serialise it as a tool result. Such a call returns a clear
error result rather than empty output. Expose a buffered-response variant of
the endpoint if an agent needs the data.

## The MCP context

A tool handler (or one of its dependencies) may declare a parameter typed
`MCPContext`, or named `ctx` / `context`, to receive the per-call context. It
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
`initialize`, `tools/list`, and `tools/call` and respond on stdout.

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
