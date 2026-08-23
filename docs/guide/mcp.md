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
schemes, `Depends`, and request middleware all run on the agent-facing call too,
so exposing a route never bypasses its authorization.

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

!!! note "Only the request phase is replayed"
    [`Middleware.process_response`](middleware.md#veloce-middleware-vs-asgi-middleware)
    does not run on an MCP call. A tool result is derived from the response body,
    not from a wire response, so a middleware that decorates the response —
    `GZipMiddleware`, `SecurityHeadersMiddleware`, `ConditionalGetMiddleware` —
    has nothing to act on. One that spans both phases still does its request-phase
    work: `SessionMiddleware` loads the session for the handler, but writes no
    cookie back, since an MCP client has no cookie jar.

    Dispatch-shape middleware — `@app.middleware("http")` and
    [`BaseHTTPMiddleware`](middleware.md#class-based-middleware) — is not replayed
    either. It wraps the ASGI request, and a replayed tool call never becomes one.
    Put logic that must run on both doors in a
    [`Middleware`](middleware.md#veloce-middleware-vs-asgi-middleware) subclass.

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
  tool.

    A pure `@app.mcp_tool` has no HTTP method to derive from, so it declares its
    own hints instead:

    ```python
    @app.mcp_tool(
        description="Delete a widget",
        annotations={"destructiveHint": True, "idempotentHint": True},
    )
    async def delete_widget(widget_id: str) -> str:
        ...
    ```

    Declaring nothing is still meaningful: the spec's defaults are the cautious
    reading (destructive, open-world), so an undeclared tool is never assumed
    safe. A hint outside the spec's set is refused at the decorator rather than
    sent as wire data no client reads. A route-backed tool may pass
    `annotations=` too, to correct a hint its verb implies.
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

### More than one content block

A tool result carries a *list* of content blocks, so a handler with more than one
thing to say - a caption beside a figure, a chart beside the rows behind it -
returns the blocks it wants and they reach the client in order:

```python
from veloce import Veloce
from veloce.contrib.mcp import ImageContent, ResourceLink, TextContent

app = Veloce()


@app.mcp_tool(description="Chart the quarter with the figures behind it")
async def revenue_chart(quarter: str):
    png_bytes_base64 = "iVBORw0KGgo="  # ... your rendered PNG, base64-encoded
    return [
        TextContent(f"Revenue for {quarter}"),
        ImageContent(png_bytes_base64, "image/png"),
        ResourceLink(f"file://revenue-{quarter}.csv", "figures"),
    ]
# tools/call -> content: [{"type": "text", ...}, {"type": "image", ...},
#                         {"type": "resource_link", ...}]
```

`TextContent`, `ImageContent`, `AudioContent`, `ResourceLink` and
`EmbeddedResource` are importable from `veloce.contrib.mcp`; each takes an
optional `annotations=` mapping (`audience`, `priority`, `lastModified`). A
single block may be returned on its own, without a list.

Every item must be a content block. A list mixing blocks with plain data is
reported as an error naming the offending position, rather than serialising an
object into text. Any return that is not a block is data and shapes exactly as it
did - a `dict`, a scalar, or a plain list is unaffected.

!!! note
    Blocks are for a handler reached through the MCP door. A route-backed tool
    builds an HTTP response, so it points at non-text content with the
    `X-MCP-Resource-Link` / `X-MCP-Embedded-Resource` headers below, or by
    returning an image/audio `Response` as above.

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
does, so its `Depends`, `Security`, request middleware, and `response_model` all
run — a field outside the response model never reaches the agent, and a guard
that rejects the call fails the read. The response body becomes the resource
contents: a JSON or `text/*` body is returned as `text`, and any other media type
(an image, a binary file) as a base64 `blob`.

### Binding a whole path to one variable

`{user_id}` is RFC 6570 *simple expansion*: it matches one URI segment, the
granularity a route path parameter occupies. A value carrying a character the URI
syntax reserves arrives percent-encoded and is decoded before the handler sees it
(`file://a%2Fb.py` binds `a/b.py`).

A file tree needs the other form. `{+name}` is *reserved expansion*: the value
carries `/` literally, so a whole path binds to one variable:

```python
@app.get(
    "/docs/{path}",
    expose_as_mcp_resource=True,
    mcp_resource_uri="docs://{+path}",
    mcp_description="Any document in the tree",
)
async def document(path: str) -> dict:
    return {"path": path}
# resources/read {"uri": "docs://guide/deep/note.md"} -> path="guide/deep/note.md"
```

When more than one template matches a URI, the most specific one wins — the one
spelling out more of the URI in literal text. So `docs://{+path}/meta` serves a
metadata read while `docs://{+path}` serves everything else, whichever order they
were registered in. A static URI is matched before any template.

### Advertising a media type

`resources/list` carries a `mimeType` when the route declares one, so an agent can
tell what a resource holds without reading it first:

```python
from veloce import Veloce

app = Veloce()


@app.get(
    "/readme",
    expose_as_mcp_resource=True,
    mcp_resource_uri="docs://readme",
    mcp_description="Project readme",
    mcp_resource_mime_type="text/markdown",
)
async def readme() -> dict:
    return {"body": "# Veloce"}
# resources/list -> {"uri": "docs://readme", "mimeType": "text/markdown", ...}
```

A declared type is authoritative: `resources/read` reports the same value, so the
listing and the read can never disagree. Declaring `response_class=HTMLResponse`
supplies the type too. A route that declares neither carries no `mimeType` at all
rather than a guess — the response class is chosen from the handler's actual
return value, so a type inferred from its annotation could contradict the read.

!!! note "Added in version 0.16"

!!! warning "Resources are read-only"
    Only a `GET`/`HEAD` route may be a resource; exposing a mutating route this way
    raises at startup. Expose a mutating route as a tool
    (`expose_as_mcp_tool=True`) instead. As with tools, exposure is
    default-closed: a route is a resource only when its author opts in, and an
    `mcp_description` is required.

The server advertises the `resources` capability only when at least one resource
is registered.

### Subscriptions

A client may **subscribe** to a resource URI and be notified when that resource
changes, so the agent re-reads only what moved. Subscriptions are opt-in: set
`MCP_RESOURCE_SUBSCRIPTIONS` in the app config before mounting.

```python
from veloce import Veloce

app = Veloce()
app.config["MCP_RESOURCE_SUBSCRIPTIONS"] = True


@app.get(
    "/settings",
    expose_as_mcp_resource=True,
    mcp_resource_uri="config://app/settings",
    mcp_description="The application settings",
)
async def settings() -> dict:
    return {"debug": False}
```

With the flag on, the `resources` capability advertises `subscribe: true` and
`listChanged: true`, and the server answers `resources/subscribe` and
`resources/unsubscribe` (each carrying a `uri`). A subscription is per-connection,
recorded on the connection's `MCPSession`.

The framework cannot know when your data changes, so signal a change from the app —
typically from the same handler that mutated the data. The server fans the
notification out to every connection subscribed to that URI:

```python
from veloce.contrib.mcp.server import MCPServer

server = MCPServer(app)


@app.mcp_tool(description="Toggle debug mode")
async def set_debug(on: bool) -> str:
    # ... mutate the settings store ...
    await server.notify_resource_updated("config://app/settings")
    return "updated"
```

Call `server.notify_resources_list_changed()` when the *set* of resources changes
(one is added or removed); it sends `notifications/resources/list_changed` to every
open connection. Both signals are no-ops when subscriptions are disabled, so the
default path stays inert.

!!! note "Added in version 0.9"
    Resource subscriptions require a stateful connection: the stdio transport, or
    the HTTP transport with `sessions=True` (an `Mcp-Session-Id` connection).
    Over such a connection the `resources` capability advertises `subscribe: true`
    and `listChanged: true`, and `notifications/resources/updated` is delivered on
    the connection's open SSE stream. A stateless HTTP request (the default, no
    `sessions=True`) advertises `subscribe: false` and rejects a `resources/subscribe`,
    since there is no connection to deliver updates over.

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

### Sending metadata back with a result

`ctx.result_meta` is the `_meta` this call's result carries. The protocol reserves
that field for metadata it does not define itself, so it is where a handler puts
what its client has agreed to read — a cost, a trace id, an extension's own block:

```python
@app.mcp_tool(description="Summarise a document")
async def summarise(text: str, ctx: MCPContext) -> dict:
    ctx.result_meta["io.example/cost"] = 0.02
    return {"summary": text[:80]}
# tools/call -> {"content": [...], "_meta": {"io.example/cost": 0.02}}
```

It belongs to one call: the next starts empty, and a handler that never touches it
sends no `_meta` at all. To describe the *tool* rather than a call of it, declare
`meta=` at registration instead — see [Tool metadata](#tool-metadata).

### Per-call scratch space

`ctx.state` is the state of the request being handled, so a handler holding the
context can stash a value without also declaring a `Request` parameter. A
dependency writing through `request.state` and a handler reading through
`ctx.state` see the same store:

```python
from veloce import Depends, MCPContext, Request, Veloce

app = Veloce()


def audit(request: Request) -> str:
    request.state.trail = ["dependency"]
    return "audited"


@app.mcp_tool(description="Run a job with an audit trail")
async def run_job(steps: int, ctx: MCPContext, _a: str = Depends(audit)) -> dict:
    ctx.state.trail.append("handler")
    return {"steps": steps, "trail": ctx.state.trail}
```

!!! note
    State lives for one call. A later call starts with an empty store, so use a
    database, cache, or session for anything that must outlive the call.

Reading `ctx.state` outside a call - on a context you constructed yourself -
raises `RuntimeError`, because there is no request to read.

### Progress and logging

`ctx.debug(...)`, `ctx.info(...)`, `ctx.warning(...)` and `ctx.error(...)` are
shorthands for the matching `ctx.log(level, ...)` call.

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

### Who is calling

The context reports the connection it is serving, so a tool can adapt to the client
without the agent passing anything:

```python
from veloce import MCPContext, Veloce

app = Veloce(title="Ops")


@app.mcp_tool(description="Describe the calling session")
async def whois(ctx: MCPContext) -> dict:
    return {
        "session": ctx.session_id,
        "client": ctx.client_info.get("name"),
        "can_sample": ctx.client_supports("sampling"),
        "backgrounded": ctx.is_background_task,
    }
```

- `session_id` — the dispatching connection's id, or `None` on the stateless path.
- On the modern revision there is no `initialize`: the client states its identity and
  capabilities in every request's `_meta`, and these read from there instead. Nothing
  in your handler has to know which revision the caller speaks.
- `client_info` — the client's `implementation` block from `initialize`.
- `client_capabilities` — what the client advertised, and `client_supports("a.b")`
  to test one, nested with dots.
- `is_background_task` — whether this call is running as a task rather than inline.
- `client_id` — the authenticated caller's id, or `None` when the call is
  unauthenticated. `client_info` is what the client *said* it was; this is what it
  proved.
- `request_id` — the JSON-RPC id of the call being served.
- `task_id` — the handle the client polls with, when this call is running as a task;
  `None` inline.
- `origin_request_id` — the id of the `tools/call` that created the task. A task
  outlives that call, so its own `request_id` is not what the client is correlating
  against.
- `transport` — `"stdio"`, `"http"` or `"sse"`; `None` off a transport. For logging
  and diagnostics — to decide whether a server-initiated request can reach the
  client, ask `client_supports(...)`.
- `lifespan_context` — the application state established at startup, the same
  `app.state` an HTTP handler reaches, so a connection pool opened in a lifespan hook
  is reached the same way through either door.

### Reading the server's own resources and prompts

A tool can read a resource or render a prompt that the same server exposes:

```python
from veloce import MCPContext, Veloce

app = Veloce(title="Ops")


@app.get(
    "/config",
    expose_as_mcp_resource=True,
    mcp_resource_uri="config://app",
    mcp_description="Runtime configuration",
)
async def config() -> dict:
    return {"theme": "dark"}


@app.mcp_tool(description="Summarise the current configuration")
async def summarise(ctx: MCPContext) -> dict:
    contents = await ctx.read_resource("config://app")
    return {"read": contents, "available": [r["uri"] for r in ctx.list_resources()]}
```

`read_resource` and `get_prompt` go through the same handlers `resources/read` and
`prompts/get` serve, **including their scope checks**. A tool cannot reach a resource
its caller could not have read directly — the authorization is not bypassed by going
through a tool.

`list_resources()` and `list_prompts()` return what the corresponding list method
reports. `await ctx.send_notification(method, params)` sends an arbitrary JSON-RPC
notification, and is inert when no channel is wired.

!!! note "Added in version 0.16"

    `session_id`, `client_info`, `client_capabilities`, `client_supports`,
    `is_background_task`, `debug`/`info`/`warning`/`error`, `read_resource`,
    `get_prompt`, `list_resources`, `list_prompts` and `send_notification`.

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

### Handler output on stdio

Over stdio the process's standard output *is* the protocol pipe, so anything else
written there would be injected into the JSON-RPC stream as a line the client
cannot parse. Veloce isolates the wire for the duration: the protocol is carried
on private duplicates of descriptors 0 and 1, while descriptor 0 points at the
null device and descriptor 1 at stderr. Both are restored when serving ends.

In practice this means a `print` left in a handler, a library that logs to stdout,
and a subprocess a tool spawns all land on **stderr**, where they are visible as
diagnostics instead of corrupting the protocol — and a handler that reads
`sys.stdin` sees end-of-file rather than eating the next request.

!!! tip "Prefer `ctx.log` over `print`"
    A message meant for the client belongs on the MCP logging channel:

    ```python
    from veloce import MCPContext

    @app.mcp_tool(description="Rebuild the index")
    async def reindex(ctx: MCPContext) -> dict:
        await ctx.log("starting")
        return {"ok": True}
    ```

### Call timeout

The stdio transport serves calls one at a time, so a handler that blocks forever
would wedge every later call. Set `app.config["MCP_CALL_TIMEOUT"]` to a number of
seconds to bound each call: a call that overruns it is cancelled and surfaced as an
error (in-band `isError` for a tool, a JSON-RPC error for a resource read or
prompt). It is unset (no timeout) by default.

### Server-initiated requests

Over the stdio transport the connection is bidirectional, so a tool may call back
into the client mid-handler. `MCPContext` exposes three server-initiated requests,
each gated on the client having advertised the matching capability in `initialize`
— a call against a client that did not advertise it raises `MCPCapabilityError`, surfaced as an `isError` tool result so the
agent can carry on without it.

`ctx.sample(...)` asks the client's LLM to produce a completion
(`sampling/createMessage`). It takes the message list and `max_tokens`, plus
optional `model_preferences`, `system_prompt`, `temperature`, and `stop_sequences`;
`tools` / `tool_choice` enable tool-using sampling and require the client's
`sampling.tools` sub-capability:

```python
from veloce import MCPContext

@app.mcp_tool(description="Summarise text with the client's model")
async def summarise(text: str, ctx: MCPContext) -> str:
    result = await ctx.sample(
        [{"role": "user", "content": {"type": "text", "text": text}}],
        max_tokens=128,
        model_preferences={"intelligencePriority": 0.9},
    )
    return result["content"]["text"]
```

#### Sampling with tools

A model given tools rarely answers in one round trip: it answers with a request
to call one, and the handler has to execute it, append the result and ask again
before it has an answer. `ctx.sample_with_tools(...)` runs that loop over tools
this server already has:

```python
from veloce import MCPContext, Veloce

app = Veloce(title="Support")


@app.mcp_tool(description="Look up an order")
async def order(order_id: str) -> dict:
    return await db.order(order_id)


@app.mcp_tool(description="Answer a customer question")
async def answer(question: str, ctx: MCPContext) -> str:
    run = await ctx.sample_with_tools(
        [{"role": "user", "content": {"type": "text", "text": question}}],
        tools=["order"],
        max_tokens=512,
    )
    return run.text
```

Every tool the model asks for runs through the same path `tools/call` serves —
declared scopes, call hooks, timeout and error shaping included — and its result
is fed back as the next message. The returned `SamplingRun` carries `text`, the
raw `content` blocks, `model`, `stop_reason`, the full `messages` transcript
(ready to extend for another run), `tool_calls` (each a `SampledToolCall` with
the arguments the model chose and whether the call failed), and `rounds`. The
transcript closes with the answer, so extending it for a follow-up run carries
the reply along. A tool that declares an output shape sends it back on the
`structuredContent` channel, the same one a direct `tools/call` caller reads.

`tools=` is a restriction, not a hint: a tool outside that list — or one that
does not exist — comes back to the model as an error result instead of being
executed. Naming a tool this server does not register is refused outright, since
it would otherwise silently offer nothing.

A failing tool call is reported to the model rather than raised: it asked for the
call and can correct itself given the reason, whereas raising would end the
handler on an argument the model generated.

`max_tool_rounds` (5 by default) caps how many times tools are executed. On the
round after the cap the model is asked to answer without tools, so a run ends
with an answer rather than an unanswered request.

!!! warning "Requires the `sampling.tools` sub-capability"
    Tool-using sampling is a modern-revision feature. A client advertising
    `sampling: {}` cannot receive tools, so the call raises `MCPCapabilityError`
    rather than putting a request on the wire it cannot act on.

`ctx.elicit(...)` asks the client to gather input from its user
(`elicitation/create`). Form mode passes a `requested_schema` (the JSON Schema of
the fields to collect); URL mode passes a `url` the client opens instead:

```python
@app.mcp_tool(description="Confirm a destructive action")
async def delete_all(ctx: MCPContext) -> dict:
    answer = await ctx.elicit(
        "Delete every record?",
        requested_schema={"type": "object", "properties": {"confirm": {"type": "boolean"}}},
    )
    return {"action": answer["action"]}
```

URL mode additionally carries an `elicitationId` naming the interaction, which is
minted for you. Pass `elicitation_id=` to reuse an identifier the URL flow already
knows, so a later `notifications/elicitation/complete` (sent with
`ctx.send_notification`) can be matched to it:

```python
@app.mcp_tool(description="Authorize this server against an upstream API")
async def authorize(ctx: MCPContext) -> dict:
    answer = await ctx.elicit(
        "Authorize access", url="https://example.com/oauth/start", elicitation_id="flow-42"
    )
    return {"action": answer["action"]}
```

!!! warning "URL mode requires the client to have declared it"
    A client advertises `elicitation: {"url": {}}` to accept URL mode. One
    advertising `elicitation: {}` — or only `{"form": {}}` — is telling you it
    handles forms and nothing else, so sending it a URL raises
    `MCPCapabilityError` rather than putting a request on the wire the client
    cannot act on. Form mode is never gated this way, since the empty capability
    is exactly what a form-only client sends.

`ctx.roots()` lists the filesystem roots the client exposes (`roots/list`),
returning the `roots` array:

```python
@app.mcp_tool(description="List the client's workspace roots")
async def workspace(ctx: MCPContext) -> list[dict]:
    return await ctx.roots()
```

These require a bidirectional transport (stdio). Calling one off such a transport
raises `RuntimeError`. The HTTP transport's per-POST model has no return channel
for server-initiated requests, so they are stdio-only.

!!! note "Added in version 0.9"
    Server-initiated `sample` / `elicit` / `roots` require the client to advertise
    the matching capability; existing one-way tools are unchanged.

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
- `tasks/list` — list the calling connection's own tasks.
- `tasks/cancel` — cancel a running task.

A task is private to the connection that created it. Over the HTTP transport with
`sessions=True`, one client's `tasks/list` shows only its own tasks, and another
client cannot `tasks/get` / `result` / `cancel` a task it does not own — the id is
treated as unknown.

!!! warning "HTTP task support requires `sessions=True`"
    A task is reachable only from the connection that created it, so a
    task-augmented call over the HTTP transport needs a persistent session.
    `mount_mcp(transport="http", sessions=True)` is required when any tool sets
    `task_support=True`; the stateless default would mint a fresh connection per
    request, leaving the task unretrievable, so it raises `ValueError` at mount
    time instead.

On the stdio transport a tool may use tasks freely, but a task runner cannot
issue a server-to-client request (`ctx.sample` / `ctx.elicit` / `ctx.roots`):
stdio has a single reader, and the serve loop resumes reading once the task is
created, so the runner has no channel for the reply. Such a call settles the task
as a failed result with an actionable message. Call the tool synchronously (no
`task` field) when it needs sampling, elicitation, or roots over stdio.

The server emits `notifications/tasks/status` on each transition (carrying the
`io.modelcontextprotocol/related-task` `_meta` key), and the `CreateTaskResult`
returned at creation carries the `io.modelcontextprotocol/model-immediate-response`
hint. A task is retained for a bounded time-to-live (the client may set `ttl` in
milliseconds on the `task` field); a settled task is evicted once it expires, and
a session's tasks — settled or still working — are reclaimed when its
`Mcp-Session-Id` is terminated or evicted, so an abandoned task cannot leak.

!!! note "Added in version 0.9"
    Task-augmented tool calls require a tool to opt in with `task_support=True`;
    every other tool is unchanged.

## Raising a typed error

A handler can raise a typed error instead of returning a result. The typed
errors — `MCPError` and its subclasses `InvalidParamsError`,
`MethodNotFoundError`, `ResourceNotFoundError`, `InvalidRequestError`, and
`InternalError` — each carry a JSON-RPC 2.0 error code, take a message, and accept
`data=` to attach a structured payload to the error object. `AuthorizationError`
is the exception: it takes the required scope set
(`AuthorizationError(frozenset({"admin"}))`) rather than a message or `data=`,
and reports the forbidden code with those scopes:

```python
from veloce import Veloce
from veloce.contrib.mcp import InvalidParamsError

app = Veloce()


@app.mcp_tool(description="Divide two integers")
async def divide(a: int, b: int) -> float:
    if b == 0:
        raise InvalidParamsError("b must not be zero")
    return a / b
```

Any `MCPError` subclass a handler raises — from a tool, a prompt or a resource
read, and whether or not the tool is route-backed — surfaces as a JSON-RPC error
object carrying the code, message and `data` the author gave it. That is what
lets a handler answer with a code the spec assigns, such as the `-32042` that
asks the client to complete a URL elicitation and retry.

Any *other* exception is an execution failure: it is reported in-band as an
`isError` tool result so the agent can read it, with the message redacted unless
the app is in debug mode. Two failures are execution failures despite being
`MCPError` subclasses, and are reported in-band too: an argument that fails
validation, and `MCPCapabilityError` — the caller's request was well formed, and
what failed is something the tool tried while running it, which the agent can
act on.

An argument whose JSON type contradicts the tool's published schema is refused
before the handler runs, with a message naming the argument and both types
(`Invalid value for city: expected a string, got a number`) — the model wrote the
JSON, so it is the one that can fix it.

The types follow JSON Schema: a declared `integer` accepts a number whose
fractional part is zero (`3` and `3.0`) and refuses one that would lose it
(`2.5`); a declared `boolean` accepts only `true` and `false`, since a number or
a string would otherwise become an answer nobody sent. A string that a declared
number accepts (`"12"`) still coerces, as it does over HTTP — a common and
harmless model slip.

## Hooks around every call

A tool exposed from a route replays the HTTP request lifecycle, so `before_request`
and `after_request` already see it. A tool registered with `@app.mcp_tool` has no
route, so nothing ran around it — there was nowhere to put an audit log, a rate
limit, or an authorization check covering every tool a server exposes.

`@app.before_mcp_call` and `@app.after_mcp_call` run around every MCP call —
tool, resource read, or prompt render — whichever way the primitive was
registered:

```python
from veloce.contrib.mcp import AuthorizationError

@app.before_mcp_call
async def audit(name, arguments):
    logger.info("mcp call", extra={"primitive": name})
    if name.startswith("admin_") and not caller_is_admin():
        raise AuthorizationError("mcp:admin")   # refuses the call

@app.after_mcp_call
async def redact(name, result):
    return scrub(result)
```

A `before` hook returning anything other than `None` answers the call with that
value and the handler never runs — the same short-circuit shape `before_request`
has, which is what makes a cache or a feature flag expressible. An `after` hook
receives the handler's return value and returns what to send on; several chain in
registration order, and none runs if the call raised.

Raising `AuthorizationError` refuses the call as a protocol-level error. Any other
exception is reported in band like a failing handler, so a bug in a hook does not
look like a transport fault.

!!! note
    These are the *server-wide* seam. Per-tool concerns are still better expressed
    with `Depends()` and declared `mcp_scopes`, which say what one tool needs
    rather than what every tool pays.

!!! note "Added in version 0.16"
    `before_mcp_call` and `after_mcp_call`.

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

## Publishing more than one version of a tool

A tool's contract changes and the old shape still has callers. Registering both
means two names in the catalogue — two entries the agent reads and has to choose
between — and Veloce refuses a duplicate name outright.

Declare a `version` instead. Registrations sharing a name and differing in
version are all kept; the highest is the one listed, and the one a call naming no
version reaches:

```python
from veloce import Veloce

app = Veloce(title="Search")


@app.mcp_tool(name="search", description="Search the index", version="1.0")
async def search_v1(q: str) -> dict:
    return await backend.search(q)


@app.mcp_tool(name="search", description="Search the index", version="2.0")
async def search_v2(query: str, limit: int = 10) -> dict:
    return await backend.search(query, limit)
```

`tools/list` reports one `search` — v2's description and schema — carrying what
is available:

```json
{
  "name": "search",
  "description": "Search the index",
  "_meta": {"veloce": {"version": "2.0", "versions": ["1.0", "2.0"]}}
}
```

A caller reaches an earlier version by naming it in the request's `_meta`, the
spec's own extension point (there is no version field on `tools/call`):

```json
{
  "method": "tools/call",
  "params": {
    "name": "search",
    "arguments": {"q": "cats"},
    "_meta": {"veloce": {"version": "1.0"}}
  }
}
```

Each version keeps its own registration — its schema, its scopes, its
annotations — so reaching one never borrows another's. A version this server
does not have is refused by name and version rather than silently answered by
the latest.

What a tool advertises is what this server can dispatch. A tool copied from a
versioned one — by `derive_tool`, by a namespaced `mount(expose_mcp=True)`, or
by `add_mcp_proxy` — carries its own version but not the set, because the copy
is one implementation and the registry it lands in holds no siblings for it.

Versions are ordered the way semantic versioning orders them. Dotted integers
compare numerically, so `10.0` follows `2.0` rather than preceding it as a string
sort would, and a suffixed label sits below the release it is a suffix of — a
call naming no version reaches `1.0.0`, not `1.0.0-beta`. The numbers decide
first, so `2.0.0-rc` still outranks `1.0.0`. Among suffixes of one release the
comparison is textual, which orders `alpha` before `beta` before `rc` without
claiming to implement precedence rules beyond that. A label with no numeric stem
sorts below every numeric one, so an ordering exists whatever you write.

Two registrations sharing a name are still refused when they share a version, or
when only one of them declares one — without a version on both there is nothing
to order, so there is no answer to which is current.

!!! note "Versioning covers `@app.mcp_tool`"
    A route-backed tool's version is its HTTP API's version, which the route path
    already carries (`/v2/search`) and which produces a distinct tool name.

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

## Composing a surface from several sources

A server's catalogue does not have to come from one app. Three pieces compose:
mounted sub-apps contribute their own tools, `add_mcp_proxy` serves another MCP
server's tools as if they were local, and `derive_tool` republishes a tool that
already exists through a narrower surface.

### Tools from a mounted sub-app

`app.mount(prefix, sub_app)` mounts an app's routes. Pass `expose_mcp=True` and
the sub-app's MCP tools, resources and prompts join the parent's catalogue,
namespaced so two sub-apps offering the same name stay distinct:

```python
from veloce import Veloce

billing = Veloce(title="Billing")


@billing.mcp_tool(description="Look up an invoice")
async def invoice(invoice_id: str) -> dict:
    return {"id": invoice_id}


app = Veloce(title="Platform")
app.mount("/billing", billing, expose_mcp=True)
app.mount_mcp(transport="http")
# Listed as: "billing_invoice"
```

The namespace defaults to the mount prefix. Pass `mcp_namespace=` to choose
another, or `mcp_namespace=""` to merge the sub-app's names in unprefixed.

### Tools from another MCP server

A gateway fronting several MCP servers needs their catalogues in its own
`tools/list` and its calls forwarded. `add_mcp_proxy` discovers an upstream's
tools once and registers each as a local tool that forwards when called.

The connection stays with the application: you pass a callable that performs one
JSON-RPC request, so retries, credentials, pooling and timeouts belong to
whoever knows the deployment.

```python
from veloce import Veloce
from veloce.contrib.mcp import add_mcp_proxy

app = Veloce(title="Gateway")


async def call_upstream(method: str, params: dict) -> dict:
    response = await client.post(UPSTREAM_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    })
    return response.json()["result"]


await add_mcp_proxy(app, "search", call_upstream)
app.mount_mcp(transport="http")
```

`scopes=` requires them of a caller before any proxied tool is invoked or listed,
the same check a local tool's `scopes=` performs — a gateway is where that
matters most, since the upstream cannot see who is asking. `tags=` labels them
for a `tool_filter` policy. The caller's own `_meta` travels with each forwarded
call, so an upstream sees the progress token and any extension block; relaying
what the upstream sends back is the application's part, since it owns the client.

Discovery is I/O, so it is awaited at setup rather than hidden behind a
decorator, and it must run before `mount_mcp` builds the registry. Every
discovered tool is registered as `"{namespace}_{upstream_name}"`, and the
upstream's own `inputSchema` is published unchanged — it is the contract the
upstream will validate against, so a schema rebuilt from a Python signature
could only disagree with it. The upstream's answer is relayed as-is, so its
`isError` and its content blocks reach the caller unwrapped.

### A narrower view of a tool you already have

An internal tool is often nearly the tool worth publishing, differing only in
what an agent should see: a friendlier argument name, a credential the caller
must not supply, a limit it must not raise. `derive_tool` changes the published
surface and translates back before the original handler runs, so both tools
share one implementation:

```python
from veloce import Veloce
from veloce.contrib.mcp import ArgTransform, derive_tool
from veloce.contrib.mcp.registry import build_registry

app = Veloce(title="Search")


@app.mcp_tool(description="Search the index (internal)")
async def search(query: str, api_key: str, limit: int = 5) -> dict:
    return await backend.search(query, api_key, limit)


app.add_mcp_tool(
    derive_tool(
        build_registry(app).tools["search"],
        name="public_search",
        description="Search the public catalogue",
        arguments={
            "query": ArgTransform(name="q", description="What to search for"),
            "api_key": ArgTransform(hide=True, default=SERVER_KEY),
            "limit": ArgTransform(default=10),
        },
    )
)
app.mount_mcp(transport="http")
```

`public_search` publishes `q` and `limit`; `api_key` is gone from the schema and
supplied on every call, which is how a server-held credential stays server-held
while the tool that needs it is still exposed. The original `search` keeps its
own registration and keeps working unchanged.

An `ArgTransform` can rename (`name=`), re-document (`description=`), replace the
JSON Schema (`schema=`), supply a `default`, state `required=`, or `hide` the
argument. Hiding without a default is refused: the caller cannot supply it, so
the handler would be called without it. Naming an argument the tool does not
have is refused too, since it would silently do nothing.

`schema=` reshapes what the agent is told, not what the handler takes — the
binder still enforces the parameter's declared type. Narrowing within that type
is the point (an `enum`, a `maximum`, a `pattern`); offering a *different* type
is refused, because it would publish a contract every call following it would be
refused for. Deriving from an already-derived tool is refused for a related
reason: one translation maps a published surface to the handler's own
parameters, so a second would map onto the first rather than onto the handler.

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

Or skip the script: `veloce mcp run app:app` serves the same thing, which is what
a client's config file names as its command.

```json
{"mcpServers": {"inventory": {"command": "veloce", "args": ["mcp", "run", "app:app"]}}}
```

Point your MCP client at either as a subprocess command; it will receive
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
ordered by this rule and always pass. Turn it on to hold clients to the spec's
ordering: a client that skips the handshake has a bug, and enforcing surfaces it
while you are developing against the server rather than later.

The flag is off by default, because rejecting an un-handshaked request also
rejects the legitimate ways a server is driven without one — a test or an
application dispatching `handle_message` directly, or a client that only ever
sends one request. The stateless HTTP transport has no persistent connection to
order against, so this setting does not affect it at all.

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

    On the `2026-07-28` revision the standard request headers become mandatory and
    are checked against the body they label. Every `POST` carries
    `MCP-Protocol-Version` and `Mcp-Method`, and a `tools/call`, `resources/read`
    or `prompts/get` also carries `Mcp-Name`. A missing header, or one whose value
    disagrees with the body, is rejected `400` with JSON-RPC `-32020`
    (`HeaderMismatchError`) — an intermediary routes on the headers while the
    server executes the body, so the two must not be allowed to diverge. A
    `Mcp-Name` outside plain printable ASCII travels as
    `=?base64?<standard-base64-of-the-utf-8-bytes>?=`. Earlier revisions defined
    none of these headers and are unaffected.

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
- Each `Mcp-Session-Id` owns a real per-connection session: it records the client
  capabilities from `initialize` (so `MCPContext.sample` / `elicit` / `roots` see
  them), scopes the in-flight cancellation registry and task ownership to that one
  client, and carries its resource subscriptions. A session a client never
  `DELETE`s is reclaimed by an idle time-to-live.
- With `MCP_ENFORCE_LIFECYCLE` set, a stateful HTTP session also rejects any request
  other than `initialize` / `ping` that precedes the `notifications/initialized` ack.

#### Running more than one worker

A session lives in the worker that minted it. Behind a load balancer a client's
second request may reach a different worker, which has never seen the id and
answers `404` — the client then starts over. Either pin a session to a worker
(sticky routing on the `Mcp-Session-Id` header), or give the workers a shared
store:

```python
import json
from dataclasses import asdict

from veloce.contrib.mcp import SessionRecord


class RedisSessions:
    def __init__(self, client):
        self._client = client

    async def read(self, session_id):
        raw = await self._client.get(f"mcp:{session_id}")
        return None if raw is None else SessionRecord(**json.loads(raw))

    async def write(self, session_id, record, ttl):
        await self._client.set(f"mcp:{session_id}", json.dumps(asdict(record)), ex=int(ttl))

    async def delete(self, session_id):
        await self._client.delete(f"mcp:{session_id}")


app.mount_mcp(transport="http", sessions=True, session_backend=RedisSessions(redis))
```

Any object with those three `async` methods is a `SessionBackend` — there is no
base class to inherit. They are async because a shared store is I/O, and a
blocking call would stall the worker's event loop.

Only part of a session travels. A `SessionRecord` holds what is true wherever the
session is served: whether it has initialized, the capabilities the client
advertised, and its `clientInfo`. Subscriptions, open listen streams, background
tasks and the in-flight cancellation registry belong to the worker holding the
connection — a task cannot be cancelled from a process that is not running it — so
each worker keeps its own and a session adopted elsewhere starts with them empty.

The record is read on every request rather than cached, which is how one worker
learns that another ended the session or completed its handshake. A `DELETE`
reaching any worker ends the session everywhere; a worker reclaiming its own idle
copy does not, since another may still be serving it.

!!! note "Added in version 0.16"
    `session_backend`, `SessionBackend` and `SessionRecord`.

!!! note "Added in version 0.9"
    HTTP session management (`sessions=True`) is opt-in; the stateless default is
    unchanged and carries no per-request session bookkeeping. Even stateless, a
    `notifications/cancelled` cancels only the request from the same `POST`, never a
    concurrent client's call with a colliding JSON-RPC id.

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

## Serving the legacy SSE transport

Before Streamable HTTP there were two endpoints: a long-lived `GET` carrying
everything the server says, and a `POST` carrying everything the client says.
Some clients still speak only that wire.

```python
app.mount_mcp(transport="sse")                        # GET /sse, POST /messages
app.mount_mcp(transport="sse", path="/agent/sse", message_path="/agent/messages")
```

The client is told where to POST by the stream itself: the first frame is an
`endpoint` event whose data is the message URL, carrying the session id that ties
the two halves together.

```
GET /sse
  event: endpoint
  data: /messages?sessionId=r_LfXhDwCm1nd4I4lOoDk5x-LcnNx7It

POST /messages?sessionId=...   ->  202 Accepted, empty body

GET /sse (still open)
  event: message
  data: {"jsonrpc":"2.0","id":1,"result":{...}}
```

The asymmetry is the design: a `POST` is acknowledged `202` with **no body**, and
its JSON-RPC response arrives later on the open stream, together with any progress
or log notifications the call produced, in the order they were produced. A client
that is not reading its stream never sees its answers.

`auth=` and `allowed_origins=` work exactly as on the Streamable HTTP transport —
both endpoints are ordinary routes on this app.

!!! warning "Prefer `transport="http"` for anything new"
    This transport is deprecated. It needs two endpoints and a connection held
    open for the session's whole life, and a dropped stream loses everything in
    flight — Streamable HTTP does the same work over one endpoint and can resume.
    Session state lives in the process that opened the stream, so it does not
    survive a restart or spread across workers.

!!! note "The wire is old; the protocol revision is not"
    This is the 2024-11-05 *transport*. The handshake still negotiates the
    revisions this server implements, so a client that opens an SSE stream and
    asks for `2024-11-05` is answered with the server's own revision and decides
    whether to proceed. Serving the 2024-11-05 protocol itself is a separate
    question from serving its wire.

!!! note "Added in version 0.16"
    `transport="sse"`.

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

### Issuing the tokens yourself

The section above validates a token someone else issued — the usual arrangement,
where an identity provider is the authorization server. When there is no such
provider, Veloce can be one:

```python
from veloce import Principal, RedirectResponse
from veloce.contrib.mcp import (
    MCPAuth, MCPAuthorizationServer, register_authorization_server,
)


def authenticate(request):
    """Who is approving this? Only your application knows."""
    user = request.session.get("user")
    if user is None:
        return RedirectResponse("/login")      # sent to the browser instead
    return Principal(subject=user, scopes={"mcp:tools"})


authorization = MCPAuthorizationServer(
    issuer="https://api.example.com",
    authenticate=authenticate,
    scopes_supported=["mcp:tools"],
)
register_authorization_server(app, authorization)

app.mount_mcp(transport="http", auth=MCPAuth(
    verify=authorization.verifier(),
    resource_server_url="https://api.example.com/mcp",
    authorization_servers=["https://api.example.com"],
))
```

That serves the four endpoints an MCP client walks:

| Endpoint | What it does |
|---|---|
| `/.well-known/oauth-authorization-server` | RFC 8414 metadata — where everything else is |
| `/register` | RFC 7591 dynamic registration, so a client needs nothing arranged in advance |
| `/authorize` | Authenticates the user through your callback, returns a single-use code |
| `/token` | Exchanges the code for a token, and refreshes it later |

A client discovers this from the resource metadata the transport already serves,
so the whole flow starts from the MCP endpoint itself.

**What it enforces.** PKCE is required and only `S256` is accepted — a code
intercepted on the redirect is useless without the verifier. A code is
single-use, expires in a minute, and is bound to the client, the redirect URI and
the challenge it was issued for. `redirect_uri` must match a registered one
exactly, and an error is only ever redirected to a URI that already matched, so
this cannot be used as an open redirector. Refresh tokens rotate, so a stolen one
stops working the moment the real client refreshes. The `resource` parameter
(RFC 8707) is recorded on the token, so a token minted for one server is
identifiable as such.

**What it stores.** Tokens are opaque — 256 bits from `secrets` — and kept as
SHA-256 digests, so there is no signing key to manage and a leaked store yields
nothing that can be presented. A client secret is shown once at registration and
kept only as a digest; a public client is issued none at all, because PKCE is
what proves it.

!!! warning "The default store is a single process"
    `InMemoryAuthorizationStore` loses every token on restart and shares nothing
    between workers. Implement `AuthorizationStore` over your own database for
    anything else — the protocol is eight async methods keyed by digest.

!!! note "Prefer an identity provider when you have one"
    Running an authorization server means owning credential storage, session
    handling, and the login and consent screens that `authenticate` returns.
    Where an IdP already exists, point `authorization_servers` at it and keep
    Veloce a resource server.

!!! note "Added in version 0.16"
    `MCPAuthorizationServer`, `register_authorization_server`,
    `AuthorizationStore` and `InMemoryAuthorizationStore`.

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

An exposed route's `Depends`, `Security`, and request middleware **run** on the
agent call (the lifecycle is replayed), but the synthetic MCP request carries no
browser credential. So an app-wide auth middleware needs to step aside in two
places, each with a first-class mechanism (no path matching).

`exclude_middleware` covers the `POST /mcp` request; `request.is_mcp` covers the
replayed tool calls. Business middleware and dependencies (a DB session, request-id
injection) need no change — they run identically on both doors.

Both mechanisms address a [`Middleware`](middleware.md#veloce-middleware-vs-asgi-middleware)
subclass: `exclude_middleware` matches one by name, and `request.is_mcp` is `True`
only on a replayed call. Neither reaches dispatch-shape middleware, which is not
replayed at all and sees `request.is_mcp` as `False` on the transport request — so
an auth check written as `@app.middleware("http")` has no way to step aside. Write
it as a `Middleware` subclass instead.

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

## Deciding which tools a caller sees

A tool, prompt or resource whose declared `scopes` the caller does not hold is not
listed to that caller. Listing it would spend the agent's context on a call that
could only be refused, and the refusal is unchanged either way — an unlisted
primitive called anyway still fails with an authorization error.

That much needs no configuration. Pass a `tool_filter` to narrow the tool listing
further, by whatever policy the application has:

```python
from veloce import Veloce, Principal

app = Veloce(title="Ops")


@app.mcp_tool(description="Read service status")
async def read_status() -> dict:
    return {"up": True}


@app.mcp_tool(description="Delete a tenant", scopes=["admin"])
async def delete_tenant(tenant_id: str) -> dict:
    return {"deleted": tenant_id}


def visible(tool, principal: Principal | None) -> bool:
    """Hide anything experimental from callers without the beta grant."""
    if "experimental" in tool.tags:
        return principal is not None and "beta" in principal.scopes
    return True


app.mount_mcp(transport="http", tool_filter=visible)
```

The filter runs on every `tools/list`, receives the registered tool and the request
principal, and returns whether that caller may see it. It may be `async def`, which is
also the cheaper shape — see the last property below.

Four properties are worth knowing:

- **The declared scopes apply first.** A caller lacking a tool's `scopes` never sees
  it, whatever the filter returns — the same check `tools/call` performs, so a tool is
  never listed to someone who cannot invoke it.
- **A filter can hide, never reveal.** It narrows the scoped set; it cannot widen it.
- **Every tool carries `tags`.** A route-backed tool inherits its route's `tags`;
  a tool registered with `@app.mcp_tool(tags=[...])` declares its own. The field is
  always a `frozenset`, so one policy reads both kinds without a `None` check. Tags
  stay server-side — the protocol defines no tag field on a tool, so they are never
  published in `tools/list`.
- **Hiding is not enforcement.** An unlisted tool that is called anyway still raises
  the same authorization error. The filter controls the agent's context, not access.
- **A `def` filter runs in a worker thread; an `async def` filter does not.** Running a
  synchronous policy off the event loop is what lets it consult a database or another
  blocking service safely, but it costs one thread handoff per `tools/list` — around
  0.1 ms, independent of how many tools you have. An `async def` filter is awaited
  directly with no handoff. So write `async def` when the policy only inspects the tool
  and the principal, and `def` when it blocks.

!!! note "Added in version 0.16"

    Without `tool_filter`, listing is unfiltered and unchanged.

### Configuring it for many endpoints

An `APIRouter` applies its `tags` to every route registered on it, so one rule can
govern a whole group without touching individual routes:

```python
from veloce import APIRouter, Veloce

app = Veloce(title="Ops")
admin = APIRouter(prefix="/admin", tags=["admin"])


@admin.get("/purge", expose_as_mcp_tool=True, mcp_description="Purge stale records")
async def purge() -> dict:
    return {"purged": True}


@app.get("/status", expose_as_mcp_tool=True, mcp_description="Service status")
async def service_status() -> dict:
    return {"up": True}


app.include_router(admin)


def visible(tool, principal) -> bool:
    tags = tool.tags
    if "admin" in tags:
        return principal is not None and "ops" in principal.scopes
    return True


app.mount_mcp(transport="http", tool_filter=visible)
```

An anonymous caller now lists only `service_status`; a caller holding `ops` lists both.

!!! warning "Do not cache the result yourself"

    The filter is evaluated per request and never memoized by the framework, because
    only your application knows when a principal's grants change. If your policy
    performs a database or identity-provider lookup, cache inside the callback where
    the invalidation rules live — and prefer an `async def` filter so the lookup is
    not offloaded to a worker thread.

### Narrowing one connection's view

`tool_filter` is a policy fixed when the server is mounted. A running call can
also change what *its own* client sees — unlocking a tool once a licence is
verified, hiding a step once it is done — without touching what any other client
is served:

```python
from veloce import MCPContext, Veloce

app = Veloce(title="Onboarding")


@app.mcp_tool(description="Verify the licence key")
async def verify(key: str, ctx: MCPContext) -> str:
    await ctx.unhide("provision_tenant")
    return "verified"
```

`ctx.hide(name)` removes a tool, prompt or resource from this connection's
listings; `ctx.unhide(name)` puts it back; `ctx.reset_visibility()` restores
everything. Resources are named by their URI.

Each sends the `list_changed` notification for the listing the name belongs to,
and only when something actually changed — a name that names nothing sends
nothing. Because a connection's listings can change this way, a stateful
connection is advertised `listChanged: true` for tools, prompts and resources at
`initialize`; a stateless request, which has no connection to narrow and no
channel to be told on, is advertised `false`.

!!! warning "Hiding is not enforcement"

    A hidden primitive is still callable, exactly as with `tool_filter`. What a
    caller may invoke is decided by its declared scopes, so a hidden name must
    never be mistaken for a permission boundary.

Visibility belongs to a connection, so this needs a stateful session: on the
stateless HTTP path, where each request stands alone, `hide` raises.

## Serving a catalogue through search

Every entry `tools/list` returns lands in the agent's context window. A server
with three hundred tools spends most of that window before the agent has done
anything, and [paging](#paging-a-large-catalogue) only spreads the same cost over
more round trips.

`tool_search=True` publishes three tools in place of the catalogue:

```python
app.mount_mcp(transport="http", tool_search=True)
```

`tools/list` now answers with `search_tools`, `describe_tools` and `run_tools`,
and every other tool is reached through them. Nothing else changes: an unlisted
tool is still callable by name, with the same scopes, hooks and error shaping.

**`search_tools(query, limit=10)`** ranks the catalogue against a query — BM25
over each tool's name, title, description and tags — and returns the matches by
name with a one-line description each:

```json
[{"name": "refund_order", "description": "Refund an order in full", "score": 4.9}]
```

**`describe_tools(names)`** returns the full definition of each named tool —
description, input schema, annotations — which is what the agent needs before it
can call one.

**`run_tools(steps, stop_on_error=True)`** runs several calls in one request. A
step's argument may reference an earlier step's result, so a chain costs one
round trip instead of one per call:

```json
{
  "steps": [
    {"id": "cust", "tool": "find_customer",
     "arguments": {"email": "ada@example.com"}, "quiet": true},
    {"tool": "list_orders",
     "arguments": {"customer_id": {"$from": "cust", "path": "/id"}}}
  ]
}
```

`{"$from": "<step id>", "path": "<JSON pointer>"}` is replaced by that part of
the named step's result (RFC 6901; omit `path` for the whole result). A step
marked `quiet` is left out of the response — later steps still reference it,
which is how a chain avoids spending context on an intermediate value. A failing
step is always reported, quiet or not, and stops the run unless
`stop_on_error=false`.

!!! warning "`run_tools` executes declared calls, never code"
    Each step names a registered tool and its arguments. Nothing is compiled and
    no expression is evaluated, so no sandbox is involved and none is relied on.
    Every call goes through the same path `tools/call` serves — declared scopes,
    call hooks, the timeout and the error shaping all apply — so a tool a caller
    may not invoke is no more reachable inside a plan than it is directly, and a
    plan that is stopped by one still reports the steps that already ran.

Discovery honours the same visibility the listing does: a `tool_filter`, a
tool's declared scopes, and anything this connection hid with `ctx.hide` all
decide what `search_tools` finds and what `describe_tools` will describe. Search
stands in for the listing, so it answers alike. The three names must be free — a server already
registering one of them is told which, at mount time.

## Paging a large catalogue

Every entry a list method returns lands in the agent's context window, so a
server with a large catalogue can spend a sizeable part of it before the agent
does any work. `page_size` opts the list methods into the spec's cursor: each
answers with at most that many entries, plus a `nextCursor` while more remain.

```python
app.mount_mcp(transport="http", page_size=50)
```

```json
// tools/list -> {"tools": [ ...50 entries... ], "nextCursor": "NDk6b3BfNDk="}
// tools/list {"cursor": "NDk6b3BfNDk="} -> the next 50
```

A client walks the catalogue by passing the `nextCursor` it received back as
`cursor`, until a response arrives without one. All four list methods paginate:
`tools/list`, `resources/list`, `resources/templates/list` and `prompts/list`.
Filtering runs first, so a tool hidden by a
[visibility policy](#deciding-which-tools-a-caller-sees) never occupies a slot on
a page.

Cursors are opaque — a client must not parse or construct one, and a cursor this
server did not issue is rejected with `-32602` (invalid params).

!!! note "Pagination is opt-in"
    `nextCursor` is optional for clients as well as servers, so a client is free
    to read the first page and stop. A server that paginated uninvited would
    silently hide the rest of its catalogue from every such client. Left unset,
    every list is answered in full, exactly as before.

A cursor names the entry it stopped at, not just a position, so registering or
removing a tool part-way through a client's walk does not make it skip an
unrelated one.

## Protocol versions

MCP has two eras, and Veloce serves both from the same endpoint:

- **Modern** (`2026-07-28`) — no handshake and no protocol-level session. A
  client declares its version, identity and capabilities in `_meta` on every
  request, and the server answers each one independently.
- **Handshake** (`2025-11-25`, `2025-06-18`) — the client opens with
  `initialize`, negotiates once, and the connection carries that state.

Which era applies is decided by how the client opens: a request carrying
`io.modelcontextprotocol/protocolVersion` in `_meta` is served the modern way,
while an `initialize` request selects the handshake semantics. Nothing needs
configuring.

### Discovery

A modern client may call `server/discover` before anything else to learn what
the server supports in one request:

```json
{
  "jsonrpc": "2.0",
  "id": "d1",
  "method": "server/discover",
  "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
}
```

```json
{
  "jsonrpc": "2.0",
  "id": "d1",
  "result": {
    "resultType": "complete",
    "supportedVersions": ["2026-07-28", "2025-11-25", "2025-06-18"],
    "capabilities": {"tools": {"listChanged": false}, "logging": {}},
    "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "WeatherServer", "version": "1.0.0"}},
    "instructions": "Weather utilities."
  }
}
```

`supportedVersions` is ordered newest-first, so a client taking the head gets
the newest revision both sides speak. `instructions` comes from the app's
`description`, the same text the HTTP API documents itself with.

### Version mismatch

A request declaring a version the server does not serve is rejected with
`-32022`, naming what *is* served so the client can retry rather than fail:

```json
{
  "error": {
    "code": -32022,
    "message": "Unsupported protocol version",
    "data": {"supported": ["2026-07-28", "2025-11-25", "2025-06-18"], "requested": "1900-01-01"}
  }
}
```

Every modern result also carries `resultType: "complete"`. Handshake-era
results do not — that field belongs to the modern revision only.

!!! note "Added in version 0.15"
    `server/discover`, per-request `_meta` version selection, `resultType`, and
    the `-32022` rejection. Earlier versions served only the handshake eras, so
    a modern client's opening probe failed and it fell back to `initialize`.

## Logging, and what the modern revision removed

`ping`, `logging/setLevel` and `notifications/roots/list_changed` are not part of the
modern revision. A modern client asking for one is told the method does not exist; a
handshake-era client keeps all three.

The log level moved with them. Instead of setting it once per connection, a modern
client names it on each request:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "reindex",
    "arguments": {"shard": "a"},
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/logLevel": "warning"
    }
  }
}
```

Your handler is unchanged — `await ctx.info(...)` and friends work the same. What
changes is who hears them:

- A request naming a level receives messages at that level and above.
- **A request naming no level receives none.** The spec requires a server not to emit
  `notifications/message` for a request that did not ask for logging, so a modern
  client that wants logs must ask on every request.
- A handshake-era request is unaffected: it logs everything unless `logging/setLevel`
  narrowed it.

!!! note "Added in version 0.16"

## Tasks on the modern revision

Tasks moved out of the core protocol into the `io.modelcontextprotocol/tasks`
extension. Declaring a tool task-capable is unchanged:

```python
from veloce import Veloce

app = Veloce(title="Ops")


@app.mcp_tool(description="Rebuild the search index", task_support=True)
async def reindex(shard: str) -> dict:
    return {"reindexed": shard}
```

What changed is what a modern client sees:

- The server advertises `io.modelcontextprotocol/tasks` in `server/discover`,
  but only when some tool is task-capable.
- A client must declare the extension in its per-request capabilities before the
  server will hand it a task. A client that has not is told to call the tool without
  a `task` field, rather than being given a handle it has no methods to resolve.
- The handle comes back as `resultType: "task"`.
- The duration fields are `ttlMs` and `pollIntervalMs`.
- `tasks/list` and `tasks/result` no longer exist. A client polls `tasks/get`, and a
  completed task carries its result there.
- `tasks/update` delivers responses to a task's outstanding input requests and is
  acknowledged with an empty result.

A handshake-era client keeps all four original methods and the field names its
revision defined, so nothing that works today stops working.

!!! note "Tasks over HTTP need a session"

    A task created by one request is retrieved by a later one, so the HTTP transport
    requires `mount_mcp(transport="http", sessions=True)`. Over stdio the connection
    is the session and nothing extra is needed.

!!! note "Added in version 0.16"

## Notification streams

On the modern revision a client opens a long-lived stream with
`subscriptions/listen`, naming the notification types it wants. It replaces the
handshake-era `resources/subscribe` and the HTTP GET stream, and it is opt-in on the
same flag:

```python
from veloce import Veloce

app = Veloce(title="Ops")
app.config["MCP_RESOURCE_SUBSCRIPTIONS"] = True
```

A client asks for what it wants:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "subscriptions/listen",
  "params": {
    "notifications": {
      "toolsListChanged": true,
      "resourceSubscriptions": ["config://service"]
    }
  }
}
```

The server acknowledges first, reporting the subset it will honour, and every message
on the stream carries the subscription id — the JSON-RPC id of the request that opened
it — so a client sharing one stdio channel can tell its streams apart.

Signal a change from your application; the server delivers it only to the streams that
asked for that type. Hold the server the mount returns, then signal from wherever your
data changes:

```python
from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer

app = Veloce(title="Ops")
app.config["MCP_RESOURCE_SUBSCRIPTIONS"] = True


@app.get(
    "/config",
    expose_as_mcp_resource=True,
    mcp_resource_uri="config://service",
    mcp_description="Runtime configuration",
)
async def service_config() -> dict:
    return {"tier": "standard"}


server = MCPServer(app)


async def on_config_written() -> None:
    """Call this from whatever writes the configuration."""
    await server.notify_resource_updated("config://service")
    await server.notify_resources_list_changed()
    await server.notify_tools_list_changed()
    await server.notify_prompts_list_changed()
```

A stream ends when the client cancels it (`notifications/cancelled` naming the listen
request's id), at which point the server answers the original request with a completion
result. A transport that drops without that response tells the client the disconnect
was unexpected.

!!! note "A stream is per-connection state"

    `subscriptions/listen` needs a stateful connection — stdio, or HTTP with
    `sessions=True`. A stateless POST holds no stream and the request is rejected.

!!! note "Added in version 0.16"

## Result caching

On the modern revision, list results and `resources/read` carry the caching hints the
spec requires, so a client can avoid re-fetching a tool list on every reconnect:

```json
{
  "resultType": "complete",
  "tools": [],
  "ttlMs": 300000,
  "cacheScope": "public"
}
```

`ttlMs` is how long the client may consider the result fresh; set it with
`app.mount_mcp(cache_ttl_ms=...)`, where `0` means immediately stale.

`cacheScope` is decided for you, and it is the half that matters:

- **`public`** — the list is identical for every caller, so a shared gateway may
  serve one client's copy to another.
- **`private`** — the result can differ between callers, so it must not be shared
  across authorization contexts. Veloce marks a result private when a `tool_filter`
  is configured, when any tool or prompt declares `scopes`, or for every
  `resources/read` (whose route runs under the calling principal).

!!! warning "Scope is a hint, not a control"

    A cache scope tells clients what they may share. It is not access control —
    the per-tool and per-resource checks still run on every call, and you should not
    rely on `cacheScope` to keep anything private.

Handshake-era clients receive no hints, since those revisions have no such fields.

!!! note "Added in version 0.16"

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
- Full signatures are in the [API reference](../reference/index.md).
