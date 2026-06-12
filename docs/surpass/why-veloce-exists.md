---
description: Why Veloce exists — one compiled route contract that the runtime, OpenAPI, and the MCP tool surface are all lowered from, so they cannot drift.
---

# Why Veloce Exists

## "Isn't this just another FastAPI?"

It is a fair question. Veloce borrows the patterns you already know — typed
dependency injection, `response_model`, Flask-style `g` and blueprints — and a
fast async framework with those patterns already exists. Reinventing one for its
own sake would be hard to justify.

Veloce exists for a reason those patterns do not capture on their own: it treats
your route handlers as the **source for a compiler**. Each handler is inspected
once, at registration, into a single intermediate representation. Every interface
your app exposes — the runtime request pipeline, the OpenAPI document, the MCP
tool surface — is **lowered from that one representation**, not re-derived
independently. That is an architectural choice, not a feature, and it is the
thing the framework is built around.

## Your app is a compiler

A compiler has three parts: a source, an intermediate representation (IR), and
one or more emit targets. Veloce maps onto all three.

<div class="vl-pipeline">
  <div class="vl-stage">
    <span class="vl-stage__label">Source</span>
    <code class="vl-stage__body">@app.post<br>async def handler()</code>
  </div>
  <span class="vl-pipeline__arrow" aria-hidden="true">&rarr;</span>
  <div class="vl-stage">
    <span class="vl-stage__label">IR</span>
    <code class="vl-stage__body">HandlerPlan<br><span class="vl-dim">(per route)</span></code>
  </div>
  <span class="vl-pipeline__arrow" aria-hidden="true">&rarr;</span>
  <div class="vl-stage vl-stage--targets">
    <span class="vl-stage__label">Emit targets</span>
    <ul class="vl-stage__list">
      <li>Runtime request pipeline</li>
      <li>OpenAPI 3.1 document</li>
      <li>MCP tool surface</li>
    </ul>
  </div>
</div>

- The **source** is your handler signature — its parameters, their types and
  defaults, its dependencies, and its return type.
- The **IR** is the `HandlerPlan`, built once when the route is registered. It is
  not re-read on the request hot path.
- The **emit targets** are the interfaces that consume the plan.

Reflection happens at registration, in one place. The request path executes a
plan that was already compiled.

## What the IR holds

The `HandlerPlan` is a structured description of everything the handler consumes
and produces:

- each parameter's **type and source** — path, query, header, cookie, body, or form;
- the **dependency graph** — `Depends`/`Security` callables and their own
  sub-dependencies, grouped into parallel-safe waves and flagged for
  thread-offload where they run blocking work;
- the **response type** (`response_model`);
- the **auth requirements** and a per-route **side-effect class** (whether the
  route is read-only or mutating).

You never write the plan. You write a normal handler; the plan is the framework's
projection of it. See [Dependency Injection](../guide/dependency-injection.md)
for how the dependency portion is built and resolved.

## One handler, interfaces that agree by construction

Consider a single route:

```python
from pydantic import BaseModel

from veloce import Depends, Header, Veloce

app = Veloce()


class Item(BaseModel):
    name: str
    price: float


async def current_user(token: str = Header()) -> str:
    return lookup(token)


@app.post("/items/{item_id}")
async def create_item(item_id: int, item: Item, user: str = Depends(current_user)) -> Item:
    return item
```

From this one registration, the IR knows that `item_id` is an `int` path
parameter, `item` is a request-body model, `current_user` is a dependency that
reads a `token` header, and the response is an `Item`. Each interface is lowered
from those same facts:

- the **runtime pipeline** binds `item_id` from the path, validates the body into
  `Item`, resolves `current_user`, and serialises the return value through
  `Item`;
- the **OpenAPI document** describes `item_id` as a path parameter, the request
  body as `Item`, and the `200` response as `Item`;
- the **MCP tool surface**, when the route is exposed as a tool, advertises the
  same inputs and output, and recurses into `current_user` so the header it needs
  is part of the tool's contract.

None of these reads your signature a second time and none can disagree with the
others, because there is only one description and they are all projections of it.

## What follows from this

The architecture is not the payoff; the consequences are.

- **Contracts cannot drift.** The documented shape, the enforced shape, and the
  agent-facing shape are the same shape. There is no second code path that can
  fall out of sync with the handler — a class of bug that exists whenever a
  document is maintained alongside the code it describes.
- **The hot path is compiled, not reflected.** Signature inspection and
  dependency planning happen once at registration. This is why the request path
  is cheap; the measured numbers are on the [Performance](performance.md) page.
- **The agent door is the human door.** Because the MCP tool surface is lowered
  from the same plan as the HTTP route — same validation, same auth, same
  contract — an AI agent calling a tool and a browser calling the endpoint are,
  by construction, going through the same logic. See [MCP](../guide/mcp.md).
- **New emit targets stay in sync for free.** A generated typed client is another
  lowering of the same contract; see
  [Generating Clients & SDKs](../how-to/generate-clients.md).

## What it is not

- **It is not a DSL or a build step.** You write ordinary `async def` handlers.
  The compilation is internal and happens at import time; there is nothing extra
  to run.
- **The IR is not a public API.** `HandlerPlan` is the framework's internal
  projection. Do not import it or pin to its shape — depend on the handlers you
  write and the interfaces they emit, not on the representation in between.
- **It does not remove the work of describing your API.** Types, dependencies,
  and `response_model` are still how you say what a route accepts and returns.
  Veloce's contribution is that you say it **once**.

## Next steps

- [Performance](performance.md) — the measured cost of the compiled request path.
- [Native Server Deep Dive](native-server.md) — the server Veloce owns end to end.
- [Dependency Injection](../guide/dependency-injection.md) — how the dependency
  portion of the IR is built and resolved.
- [MCP (AI Tools)](../guide/mcp.md) — the agent-facing emit target.
