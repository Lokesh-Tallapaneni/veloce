---
description: >-
  Why Veloce is a from-scratch ASGI framework rather than a Starlette wrapper:
  one IR that the runtime, OpenAPI, and an MCP tool surface all lower from.
---

# Why I wrote an ASGI framework from scratch instead of wrapping Starlette

FastAPI is based on Starlette, and a number of newer API frameworks layer on existing ASGI foundations too. That's usually the right instinct — "don't reinvent the wheel." So when I tell people I wrote [Veloce](https://veloceframework.com) — an ASGI framework — from scratch, not on Starlette, the first question is fair: *why?*

The honest answer isn't "Starlette is bad." It's that I wanted a specific property that's hard to get when your framework is a wrapper: **one definition that the runtime, the OpenAPI schema, and an LLM tool surface all derive from — so they can't drift.**

## The drift problem

In a typical typed Python API, the same facts about an endpoint get read more than once. The runtime derives, from the handler signature, how to bind and validate parameters. The OpenAPI layer inspects it again to generate the schema. If you expose tools to an agent, something inspects it a third time. Each path re-derives "what does this endpoint accept and return," and any two of them can fall out of sync — a class of bug that exists whenever a description is maintained alongside the code it describes.

I wanted the handler signature to be compiled **once** into a single intermediate representation, and every interface to be a *lowering* of that one IR. Not three readers of the signature — one source, many emit targets.

That's much easier to guarantee when you own the whole pipeline rather than composing layers that each do their own inspection.

## Your app as a compiler

So Veloce treats your handlers as the source for a compiler:

- **Source** — your typed handler signature.
- **IR** — a `HandlerPlan`, built once at registration: the dependency graph, each parameter's type and source. (The declared response type rides alongside on the route's contract, and the side-effect class is derived from the HTTP verb; the lowerings below read both.)
- **Emit targets** — the runtime request pipeline, the OpenAPI 3.1 document, and — for routes you opt into MCP exposure — a first-party MCP (Model Context Protocol) tool surface. All lowered from the same plan.

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

From this one registration, the IR knows `item_id` is an `int` path param, `item` is a body model, `current_user` is a dependency reading a header, and the response is an `Item`. The runtime binds and validates from those facts; OpenAPI documents them; and when a route is opt-in exposed as an MCP tool, the agent sees the same inputs and output, with the same validation rules and route guards. None of them re-derives your signature independently, so they can't disagree.

In the agent era this matters more than it used to: the door a human calls through and the door an LLM calls through are, by construction, the same door.

## What it cost

I'm not going to pretend this was free. Writing a framework from scratch means re-implementing the HTTP parser, the router, the dependency resolver, sessions, and security-sensitive code (CSRF, JWT, CORS, signing, password hashing) — all surface that Starlette and Werkzeug have hardened over years and millions of deployments. Veloce is young (v0.8.0, pre-1.0), its core is Python-first so I wouldn't expect it to lead trivial-route throughput against a Cython- or Rust-cored framework (I'll let the neutral benchmark show where it lands), and it carries the maintenance burden of being batteries-included in one tree.

Those are real trade-offs. If you want maximum maturity and the deepest ecosystem today, FastAPI on Starlette is the safe choice, and I'll say so plainly.

## When the rebuild is worth it

I think it's worth it when the property you want is *architectural* — when you'd otherwise be fighting the layers underneath you to get it. "One IR, every interface, in sync" is that kind of property. It's the reason Veloce exists, and it's why it isn't another Starlette wrapper.

If that resonates, the code is at [github.com/Lokesh-Tallapaneni/veloce](https://github.com/Lokesh-Tallapaneni/veloce) and `pip install veloceframework`. I'd love to hear where you think the idea holds up — and where it doesn't.
