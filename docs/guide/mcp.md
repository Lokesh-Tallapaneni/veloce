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
hooks run before the handler, in the same order they run on the HTTP path.

A middleware or hook that returns a response (an auth `401`, for example)
short-circuits the call - the handler is not invoked, `teardown_request` still
runs, and the response becomes the tool result, surfaced as an error when its
status is `4xx`/`5xx`.

The synthetic `request` carries the wrapped route's real HTTP method and rule
path, so a handler, dependency, or hook that branches on `request.method` /
`request.path` sees the route's own values (the concrete path-parameter values
remain on `request.path_params`).

A client-supplied parameter declared inside a `Depends` dependency - including a
body model - is advertised in the tool's input schema, so `tools/list` and
`tools/call` agree on the accepted inputs. A route's rule `defaults=` fill any
handler argument the call does not supply.

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
  `destructiveHint` (true for `PUT`/`PATCH`/`DELETE`, false for the additive
  `POST`), and `openWorldHint` (false for a fully read-only route, since it
  operates only on the server's own data; omitted otherwise, where the spec
  treats the tool as open-world). The route summary is also carried as
  `annotations.title`. A client may surface a consent prompt for a destructive
  tool. A pure `@app.mcp_tool` has no HTTP method, so it carries no annotations.
- **`inputSchema`** / **`outputSchema`** - when the route declares a
  `response_model` (or the handler returns a Pydantic model), the tool advertises
  a standalone JSON Schema for its result. Both schemas declare the JSON Schema
  2020-12 dialect (`$schema`), so a strict client validates against it without
  guessing. `tools/call` then returns the result as `structuredContent` alongside
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

## Icons

A tool, prompt, or resource may carry opt-in **icons** a client renders beside
it. Pass `icons=[Icon(...)]` to `@app.mcp_tool` / `@app.mcp_prompt`, or
`mcp_icons=[Icon(...)]` on an exposed route. Each `Icon` carries a required
`src` URI plus an optional `mime_type` and `sizes` list. A primitive with no
icons emits no `icons` key, so the wire form is unchanged for tools that do not
use them.

```python
from veloce import Veloce
from veloce.contrib.mcp import Icon

app = Veloce()


@app.mcp_tool(
    description="Add two integers",
    icons=[Icon("https://example.com/add.png", mime_type="image/png", sizes=["48x48"])],
)
async def add(a: int, b: int) -> int:
    return a + b
# tools/list -> icons: [{"src": "...", "mimeType": "image/png", "sizes": ["48x48"]}]
```

!!! note "Added in version 0.9"
    Icons on tools, prompts, and resources, and the resource-link / embedded
    content blocks below.

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

### Resource-link and embedded-resource results

A tool may point an agent at a resource instead of inlining the data, or inline a
resource's contents directly. The handler signals which by setting a response
header carrying the resource URI:

- **`X-MCP-Resource-Link`** — the result is a `resource_link` block referencing
  the URI; the client follows it with `resources/read`.
- **`X-MCP-Embedded-Resource`** — the result is a `resource` block inlining the
  body's contents at that URI, so the agent reads the data with no follow-up call.

Both are opt-in: a response without either header takes the unchanged
text/structured/binary path, and the header is a harmless custom header on the
HTTP door.

```python
from veloce import Response, Veloce

app = Veloce()


@app.get("/report", expose_as_mcp_tool=True, mcp_description="The latest report")
async def report() -> Response:
    return Response(
        body=b"see resource",
        content_type="text/plain",
        headers={"X-MCP-Resource-Link": "report://latest"},
    )
# tools/call -> content: [{"type": "resource_link", "uri": "report://latest", "name": "report"}]
```

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

## Argument completion

A client can ask the server to suggest values for one argument of a prompt or a
resource template as the user types, through the MCP `completion/complete` request.
Completion is opt-in per argument: register a completer with `@app.mcp_completer`,
naming the `prompt` (by name) or `resource` (by URI template) and the `argument`.

```python
from veloce import Veloce

app = Veloce()

KNOWN_NAMES = ["ada", "alan", "grace"]


@app.mcp_prompt(description="Greet a user by name")
async def greet(name: str) -> str:
    return f"Hello, {name}!"


@app.mcp_completer(prompt="greet", argument="name")
async def complete_name(value: str, context: dict[str, str]) -> list[str]:
    return [n for n in KNOWN_NAMES if n.startswith(value)]
# completion/complete {"ref": {"type": "ref/prompt", "name": "greet"},
#                      "argument": {"name": "name", "value": "a"}}
#   -> {"completion": {"values": ["ada", "alan"], "total": 2, "hasMore": false}}
```

The completer is called with the partial `value` the user has typed and a mapping
of the sibling arguments already resolved (the request's `context.arguments`), so a
completer can narrow its suggestions. It may be `async` or sync — a sync completer
runs in the thread pool. Return a list of candidate strings, or a `CompletionResult`
to declare the full match `total` and whether more values exist:

```python
from veloce.contrib.mcp import CompletionResult


@app.mcp_completer(prompt="greet", argument="name")
async def complete_name(value: str, context: dict[str, str]) -> CompletionResult:
    matches = await directory.search(prefix=value)
    return CompletionResult(matches[:100], total=len(matches))
```

A completer for a resource template names its argument by a URI-template variable:

```python
@app.get(
    "/users/{user_id}",
    expose_as_mcp_resource=True,
    mcp_description="A user record",
    mcp_resource_uri="users://{user_id}",
)
async def get_user(user_id: str) -> dict:
    return {"id": user_id}


@app.mcp_completer(resource="users://{user_id}", argument="user_id")
async def complete_user_id(value: str, context: dict[str, str]) -> list[str]:
    return [uid for uid in active_user_ids() if uid.startswith(value)]
```

!!! note
    A single response is capped at 100 values; an over-cap return is truncated and
    `hasMore` is set so the client knows more matches exist. An argument with no
    registered completer answers with an empty completion (never an error), so a
    client may always probe. The server advertises the `completions` capability
    only when at least one completer is registered.

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

### Cancellation

When the client sends a `notifications/cancelled` naming an in-flight request, the
server cancels that call's task and marks its context cancelled. A handler blocked
on an `await` unwinds; a cooperative handler can poll `ctx.cancelled` and stop
early:

```python
@app.mcp_tool(description="A long scan the client may cancel")
async def scan(count: int, ctx: MCPContext) -> dict:
    for i in range(count):
        if ctx.cancelled:
            return {"scanned": i, "cancelled": True}
        await ctx.report_progress(i + 1, count)
    return {"scanned": count}
```

The `initialize` request is never cancellable (the spec forbids it), and a cancel
naming an already-finished or unknown request is ignored. Over the Streamable HTTP
transport a cancelled call closes its SSE stream without a response frame.

### Call timeout

The stdio transport serves calls one at a time, so a handler that blocks forever
would wedge every later call. Set `app.config["MCP_CALL_TIMEOUT"]` to a number of
seconds to bound each call: a call that overruns it is cancelled and surfaced as an
error (in-band `isError` for a tool, a JSON-RPC error for a resource read or
prompt). It is unset (no timeout) by default.

## Background tasks

A long tool call can run as a background **task**: the client sends the
`tools/call` with a `task` field, gets a task id back immediately, and retrieves
the result later. Task support is opt-in per tool — pass `task_support=True` to
`@app.mcp_tool` (or `mcp_task_support=True` on an exposed route):

```python
@app.mcp_tool(description="A long report the client can poll", task_support=True)
async def build_report(rows: int) -> dict:
    return {"rows": rows, "ready": True}
```

The same handler runs whether the call is synchronous or a task — a route stays
one handler behind every door. An opted-in tool advertises
`execution.taskSupport: "optional"` in `tools/list`; a tool that does not opt in
rejects a task-augmented call.

The client drives the task with four methods:

- `tasks/get` — poll the task's status (`working`, then `completed` / `failed` /
  `cancelled`).
- `tasks/result` — retrieve the settled `tools/call` result.
- `tasks/list` — list the server's known tasks.
- `tasks/cancel` — cancel a running task.

The server emits `notifications/tasks/status` on each transition (carrying the
`io.modelcontextprotocol/related-task` `_meta` key), and the `CreateTaskResult`
returned at creation carries the `io.modelcontextprotocol/model-immediate-response`
hint. A task is retained for a bounded time-to-live (the client may set `ttl` in
milliseconds on the `task` field); a settled task is evicted once it expires.

!!! note "Added in version 0.9"
    Task-augmented tool calls require a tool to opt in with `task_support=True`;
    every other tool is unchanged.

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

### Server identity and instructions

The `initialize` result describes the server from the same metadata that
documents the HTTP API, so both doors present it identically:

- **`serverInfo.title`** - the app `title`, the human-facing display name (the
  `name`/`version` fields still carry the identifier and version).
- **`instructions`** - the app `description` (falling back to the one-line
  `summary`), surfaced to the client's model as usage guidance.

```python
from veloce import Veloce

app = Veloce(
    title="Task Service",
    description="Call list_tasks before create_task; ids are opaque.",
)
# initialize -> serverInfo.title "Task Service", instructions the description
```

Neither field is emitted when its source is unset, so a client never sees an
empty string.

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

### Connection lifecycle

The stdio loop keeps one connection alive across many messages, so it tracks a
single session. The session records the capabilities the client advertised in
its `initialize` request, which the server can consult before relying on a client
feature.

The MCP lifecycle requires the initialization exchange to come first. To enforce
that ordering, set `app.config["MCP_ENFORCE_LIFECYCLE"] = True`: any request other
than `initialize` or `ping` that arrives before initialization completes is
rejected with a JSON-RPC invalid-request error. One-way notifications are never
ordered by this rule and always pass. The flag is off by default, so the existing
stdio behavior is unchanged unless you opt in. The stateless HTTP transport has no
persistent connection to order against, so this setting does not affect it.

!!! note "Added in version 0.9"
    `MCPSession` and `MCP_ENFORCE_LIFECYCLE` were added in version 0.9.

## Serving over HTTP

For a remote (hosted) MCP server, mount the **Streamable HTTP** transport. It adds
a single `POST` route to your app, so you serve it with any ASGI server (or
`app.run()`) like the rest of your application:

```python
from veloce import Veloce

app = Veloce()


@app.mcp_tool(description="Add two integers")
async def add(a: int, b: int) -> int:
    return a + b


app.mount_mcp(transport="http", path="/mcp")  # default path is "/mcp"

if __name__ == "__main__":
    app.run()
```

Call `mount_mcp(transport="http")` **after** registering your tools, resources, and
prompts. The client `POST`s one JSON-RPC message to the route and gets one reply:

- A request with `Accept: text/event-stream` is answered with an SSE stream that
  carries the call's progress / log notifications followed by the JSON-RPC
  response. A request without it gets a single JSON response.
- A notification (a message with no `id`) is answered with `202 Accepted` and no body.
- A `GET` on the endpoint is answered `405 Method Not Allowed` (the transport keeps
  no standalone server-to-client stream).

The SSE stream opens with a priming event and closes with a `retry` reconnect hint,
and a client dropping the stream does not cancel the in-flight call.

!!! note "Transport conformance headers"
    Pass `allowed_origins=[...]` to enable `Origin` validation (DNS-rebinding
    defense): a present `Origin` outside the allowlist is rejected `403`, while a
    missing `Origin` (a non-browser client) is allowed. A request carrying an
    `MCP-Protocol-Version` header naming a revision the server does not support is
    rejected `400`; a request with no such header is unaffected.

### Session management

The HTTP transport is stateless by default — each `POST` is an independent message.
Pass `sessions=True` to opt into the MCP `Mcp-Session-Id` lifecycle:

```python
app.mount_mcp(transport="http", sessions=True)
```

- The server assigns a fresh `Mcp-Session-Id` header on the `initialize` response.
- Every later request must echo that header. A request missing it is rejected
  `400`; a request naming a terminated (or never-issued) id is rejected `404`,
  signalling the client to start a new session.
- A `DELETE` carrying the header terminates the session (`204`); a `DELETE` for an
  unknown id is `404`. Without `sessions=True`, `DELETE` is `405`.

!!! note "Added in version 0.9"
    HTTP session management (`sessions=True`) is opt-in; the stateless default is
    unchanged and carries no per-request session bookkeeping.

### Resumable streams

When a tool call replies over SSE (the client sent `Accept: text/event-stream`), a
dropped connection normally loses any events the client had not yet received. Pass
`resumable=True` to let a client reconnect and replay only what it missed:

```python
app.mount_mcp(transport="http", resumable=True)
```

- Each streamed event carrying a payload gets an `id` encoding its originating
  stream, and the events are kept in a bounded in-memory store.
- A client that drops reconnects with a `GET` carrying the standard SSE
  `Last-Event-ID` header. The server replays only the events that **one** stream
  produced after the acknowledged id — never another stream's events — then closes.
- Without `resumable=True` a `GET` is `405` and no event ids or history are kept.

!!! note "Added in version 0.9"
    SSE resumability (`resumable=True`) is opt-in; the default keeps no event ids or
    replay history and answers a `GET` `405`.

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

```python
from veloce import current_principal


def get_current_user():
    p = current_principal()          # set by HTTP auth OR MCP transport
    if p is None:
        raise Unauthorized()
    return load_user(p.subject)
```

Both doors populate one identity. Your HTTP auth calls `set_principal(...)`; the
MCP transport sets it from the validated token. All downstream code reads the same
`current_principal()`, so authorization and identity-aware dependencies are written
once and run over HTTP and MCP alike.

### Reconciling existing middleware and dependencies

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

An exposed route's `Depends`, `Security`, and middleware **run** on the agent call
(the lifecycle is replayed), but the synthetic MCP request carries no browser
credential. So an app-wide auth middleware needs to step aside in two places, each
with a first-class mechanism (no path matching).

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

```python
app.mount_mcp(transport="http", allowed_origins=["https://app.example.com"])
```

- **Origin validation** (DNS-rebinding defense, required by the MCP transport
  spec) rejects a browser request whose `Origin` is outside the allowlist (a request
  with no `Origin`, i.e. a non-browser client, is allowed).
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
