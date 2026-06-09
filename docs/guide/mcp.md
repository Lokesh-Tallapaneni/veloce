---
description: Expose Veloce handlers as Model Context Protocol tools with mcp_tool, expose_as_mcp_tool, and mount_mcp so an AI agent can call them over JSON-RPC 2.0.
tags: [mcp, tools, agents, jsonrpc]
---

# MCP (Model Context Protocol)

Veloce can expose your handlers as [Model Context Protocol](https://modelcontextprotocol.io)
tools, so an AI agent can call them over JSON-RPC 2.0. Every Veloce route can
also be a tool an agent invokes.

The integration lives in `veloce.contrib.mcp`. It supports **tools**,
**resources**, and **prompts** over both the **stdio** transport (the framing an
MCP client uses when it launches your server as a subprocess) and the **Streamable
HTTP** transport (a mounted route, for a remote/hosted server). It negotiates the
protocol version with the client and exposes tool metadata (annotations, title,
output schema).

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

## Non-text tool content

A tool whose handler returns an `image/*` or `audio/*` response emits the matching
typed MCP content block — the bytes as base64 with their media type — instead of a
text block, so an agent receives a real image or audio result:

```python
from veloce import Response, Veloce

app = Veloce()


@app.mcp_tool(description="Render the latest chart as a PNG")
async def chart() -> Response:
    png_bytes = b"\x89PNG\r\n\x1a\n"  # ... your rendered PNG bytes
    return Response(body=png_bytes, content_type="image/png")
# tools/call -> content: [{"type": "image", "data": "<base64>", "mimeType": "image/png"}]
```

Any other media type is shaped as before (a JSON or text body becomes a text
block).

!!! note "Added in version 0.5"
    Image/audio tool content blocks and the resources primitive below.

## Resources

A **resource** is the MCP primitive for data an agent reads by URI, the
counterpart to a tool it calls. Expose a read-only (`GET`/`HEAD`) route as a
resource with `expose_as_mcp_resource=True` and an `mcp_resource_uri`. A route
with no path parameters takes a static URI:

```python
from veloce import Veloce

app = Veloce()


@app.get(
    "/settings",
    expose_as_mcp_resource=True,
    mcp_resource_uri="config://app/settings",
    mcp_description="The application settings",
)
async def settings() -> dict:
    return {"debug": False}
# resources/list -> {"uri": "config://app/settings", "name": "settings", ...}
# resources/read {"uri": "config://app/settings"} -> contents text {"debug": false}
```

A route **with** path parameters takes a URI template whose variables bind those
parameters exactly (one variable per path parameter). It is advertised through
`resources/templates/list`, and `resources/read` recovers the parameter values
from the concrete URI:

```python
@app.get(
    "/users/{user_id}",
    expose_as_mcp_resource=True,
    mcp_resource_uri="users://{user_id}",
    mcp_description="A user record",
)
async def user(user_id: int) -> dict:
    return {"id": user_id}
# resources/templates/list -> {"uriTemplate": "users://{user_id}", ...}
# resources/read {"uri": "users://42"} -> the handler runs with user_id=42
```

A resource read replays the route through the same request lifecycle a tool call
does, so its `Depends`, `Security`, middleware, and `response_model` all run — a
field outside the response model never reaches the agent, and a guard that rejects
the call fails the read. The response body becomes the resource contents: a JSON or
`text/*` body is returned as `text`, and any other media type (an image, a binary
file) as a base64 `blob`.

!!! warning "Resources are read-only"
    Only a `GET`/`HEAD` route may be a resource; exposing a mutating route this way
    raises at startup. Expose a mutating route as a tool
    (`expose_as_mcp_tool=True`) instead. As with tools, exposure is
    default-closed: a route is a resource only when its author opts in, and an
    `mcp_description` is required.

The server advertises the `resources` capability only when at least one resource
is registered.

## Prompts

A **prompt** is the MCP primitive for a reusable, parameterised message template a
user invokes. Register one with `@app.mcp_prompt(...)`: the callable's parameters
become the prompt's arguments, and its return becomes the messages `prompts/get`
returns.

```python
from veloce import Veloce

app = Veloce()


@app.mcp_prompt(description="Summarise a topic in three bullet points")
async def summarise(topic: str) -> str:
    return f"Summarise {topic} in three bullet points."
# prompts/list -> {"name": "summarise", "arguments": [{"name": "topic", "required": true}]}
# prompts/get {"name": "summarise", "arguments": {"topic": "MCP"}}
#   -> messages: [{"role": "user", "content": {"type": "text", "text": "Summarise MCP ..."}}]
```

Return a plain string for a single user message, or a list of `{"role", "content"}`
messages (with `role` either `user` or `assistant`) for a multi-turn template:

```python
@app.mcp_prompt(description="A guided code review")
async def review(language: str) -> list:
    return [
        {"role": "assistant", "content": "I'll review the code you paste next."},
        {"role": "user", "content": f"Review this {language} code for bugs."},
    ]
```

A prompt's parameters resolve exactly as a tool's do: `Depends()` and `MCPContext`
parameters are injected (and never advertised as prompt arguments), and a parameter
with a default is an optional argument. As with tools and resources, a non-empty
`description` is required, and `namespace=` prefixes the prompt name.

The server advertises the `prompts` capability only when at least one prompt is
registered.

## The MCP context

A tool handler (or one of its dependencies) may declare a parameter typed
`MCPContext` to receive the per-call context; it is matched by that type
annotation, not by the parameter's name. It carries the calling tool name and the
raw argument mapping, and channels for live progress and log notifications back to
the client.

```python
from veloce import MCPContext


@app.mcp_tool(description="Echo the calling tool name")
async def whoami(ctx: MCPContext) -> str:
    return ctx.tool_name
```

The context parameter is not part of the tool's input schema - the agent never
supplies it.

### Progress and logging

`await ctx.report_progress(done, total)` sends a `notifications/progress` message
to the client mid-call, and `await ctx.log(level, message)` sends a
`notifications/message`. Both work in tools, resource reads, and prompts:

```python
@app.mcp_tool(description="Process a batch of records")
async def process(count: int, ctx: MCPContext) -> dict:
    for i in range(count):
        await ctx.report_progress(i + 1, count)
        await ctx.log("info", f"processed record {i + 1}")
    return {"processed": count}
```

Progress is only sent when the client opts in by attaching a `progressToken` to
the call (per the MCP progress utility); without one, `report_progress` is a no-op.
Log messages use RFC 5424 levels (`debug`, `info`, `notice`, `warning`, `error`,
`critical`, `alert`, `emergency`); the client can raise the minimum with
`logging/setLevel`, and a message below it is dropped.

### Call timeout

The stdio transport serves calls one at a time, so a handler that blocks forever
would wedge every later call. Set `app.config["MCP_CALL_TIMEOUT"]` to a number of
seconds to bound each call: a call that overruns it is cancelled and surfaced as an
error (in-band `isError` for a tool, a JSON-RPC error for a resource read or
prompt). It is unset (no timeout) by default.

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
`tools/list`, `tools/call`, and — when resources or prompts are registered —
`resources/list`, `resources/templates/list`, `resources/read`, `prompts/list`, and
`prompts/get`, and respond on stdout.

The serve loop runs inside the app's lifespan, so every `@app.on_startup`
handler (database pools, `app.state`, caches) and the lifespan context manager
run before the first tool is served, and the matching shutdown runs after the
input closes - exactly as when the app is served by an ASGI server.

## Serving over HTTP

For a remote (hosted) MCP server, mount the **Streamable HTTP** transport. It adds
a single `POST` route to your app, so you serve it with any ASGI server (or
`app.run()`) like the rest of your application:

```python
import asyncio

from veloce import Veloce

app = Veloce()


@app.mcp_tool(description="Add two integers")
async def add(a: int, b: int) -> int:
    return a + b


app.mount_mcp(transport="http", path="/mcp")  # default path is "/mcp"

if __name__ == "__main__":
    asyncio.run(app.run())
```

Call `mount_mcp(transport="http")` **after** registering your tools, resources, and
prompts. The client `POST`s one JSON-RPC message to the route and gets one reply:

- A request with `Accept: text/event-stream` is answered with an SSE stream that
  carries the call's progress / log notifications followed by the JSON-RPC
  response. A request without it gets a single JSON response.
- A notification (a message with no `id`) is answered with `202 Accepted` and no body.

## Authentication and authorization

MCP authenticates at the **transport**, not per tool call, following the MCP OAuth
2.1 model: the agent presents a bearer token on every request, the server validates
it (as an OAuth 2.1 *resource server*), and per-tool **scopes** decide which tools
that token may call.

### Authenticating the HTTP transport

Pass an `MCPAuth` to `mount_mcp`. You supply a `verify` callable that validates a
bearer token and returns a `Principal` (or `None` to reject) — Veloce never parses
tokens or does crypto itself. It then validates the token on every `/mcp` request
(`401` if missing/invalid), serves the [RFC 9728](https://datatracker.ietf.org/doc/html/rfc9728)
protected-resource metadata so a client can discover the authorization server, and
publishes the `Principal` for the call.

```python
from veloce import Principal
from veloce.contrib.mcp import MCPAuth


def verify(token: str) -> Principal | None:
    claims = validate_with_your_auth_server(token)   # your logic / library
    if claims is None:
        return None
    return Principal(subject=claims["sub"], scopes=set(claims["scope"].split()))


app.mount_mcp(transport="http", auth=MCPAuth(
    verify=verify,
    required_scopes=["mcp:tools"],                    # every call needs this
    resource_server_url="https://api.example.com/mcp",
    authorization_servers=["https://auth.example.com"],
))
```

!!! warning "Validate the token's audience"
    Your `verify` MUST confirm the token was issued for *this* server (its audience
    / `resource`), per [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707). Accepting
    a token minted for another service — or forwarding it onward — is the
    spec-forbidden "token passthrough" anti-pattern.

For the **stdio** transport there is no OAuth handshake — the process is launched
locally and trusted — so pass a static identity instead:
`app.mount_mcp(principal=Principal(subject="local", scopes={"mcp:tools"}))`.

### Per-tool scopes

Declare the scopes a tool, resource, or prompt requires; a principal lacking them
is rejected (`insufficient_scope`) before the handler runs:

```python
@app.mcp_tool(description="Delete a user", scopes=["users:write"])
async def delete_user(id: int): ...

@app.get("/admin/stats", expose_as_mcp_tool=True,
         mcp_description="Service stats", mcp_scopes=["admin:read"])
async def stats(): ...
```

### The unified principal

Both doors populate one identity. Your HTTP auth calls `set_principal(...)`; the
MCP transport sets it from the validated token. All downstream code reads the same
`current_principal()`, so authorization and identity-aware dependencies are written
once and run over HTTP and MCP alike:

```python
from veloce import current_principal


def get_current_user():
    p = current_principal()          # set by HTTP auth OR MCP transport
    if p is None:
        raise Unauthorized()
    return load_user(p.subject)
```

### Reconciling existing middleware and dependencies

An exposed route's `Depends`, `Security`, and middleware **run** on the agent call
(the lifecycle is replayed), but the synthetic MCP request carries no browser
credential. So an app-wide auth middleware needs to step aside in two places, each
with a first-class mechanism (no path matching):

```python
class AuthMiddleware(Middleware):
    async def process_request(self, request):
        if request.is_mcp:           # a replayed tool call — transport already authed
            return None
        ...                          # your normal HTTP session/cookie check

app.add_middleware(AuthMiddleware)

# Drop the same middleware from the /mcp transport route (it has its own MCPAuth):
app.mount_mcp(transport="http", auth=MCPAuth(...), exclude_middleware=["AuthMiddleware"])
```

`exclude_middleware` covers the `POST /mcp` request; `request.is_mcp` covers the
replayed tool calls. Business middleware and dependencies (a DB session, request-id
injection) need no change — they run identically on both doors. (`exclude_middleware`
matches `Middleware`-class middleware by name; a dispatch-style `@app.middleware("http")`
wrapper should check `request.is_mcp` itself.)

!!! warning "Tool arguments are not credentials"
    Veloce does **not** seed an agent's tool arguments into the synthetic request's
    headers or cookies, so a `Security` scheme that reads a header or cookie
    (`HTTPBearer`, `APIKeyHeader`, `APIKeyCookie`) cannot be satisfied by agent
    input — it simply sees nothing. Tool arguments *do* feed `query` and `form`
    (those are legitimate `Query(...)` / `Form(...)` input channels), so an
    `APIKeyQuery`-style scheme would read agent-controlled input. The rule stands:
    **MCP authorization comes from the validated `Principal` and `mcp_scopes`,
    never from a request header, cookie, or query value.**

### Hardening the HTTP transport

- **Origin validation** (DNS-rebinding defense, required by the MCP transport
  spec): `app.mount_mcp(transport="http", allowed_origins=["https://app.example.com"])`
  rejects a browser request whose `Origin` is outside the allowlist (a request with
  no `Origin`, i.e. a non-browser client, is allowed).
- **`MCPAuth` requires** `resource_server_url` and at least one
  `authorization_servers` entry — the metadata a compliant client needs to
  audience-bind and obtain a token.
- An insufficient-scope failure surfaces as an HTTP **403** with a
  `WWW-Authenticate` scope challenge over the JSON transport.

## Instrumentation

Each tool call fires the same `app.add_instrumentation` hooks an HTTP request
does, so a metrics exporter records tool usage and error rates with no extra
wiring. The `RequestMetrics` record carries:

- `method="tools/call"`, with `route` and `path` set to the tool name.
- the call duration.
- the call's real `status_code` — the shaped response's status for a
  route-backed or short-circuited call, `500` for an unhandled handler error,
  `200` only on genuine success.

## Next steps

- [Dependency injection](dependency-injection.md) — `Depends()` and `Security()`, which run in tools exactly as in routes.
- [Request models](request-models.md) — body models that become a tool's input schema.
- Full signatures are in the [API reference](../reference.md).
