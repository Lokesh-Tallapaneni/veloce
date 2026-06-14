---
description: >-
  How Veloce compiles each handler's dependency graph once at registration into
  a reflection-free dispatch path — and an honest account of what it buys you.
---

# Reflection-free request dispatch: how Veloce compiles the dependency graph

Typed Python web frameworks do a surprising amount of work *per request* just to figure out how to call your handler: inspect the signature, resolve type hints, walk the dependency tree, coerce and validate each parameter. It's convenient, and for I/O-bound apps the cost is usually dwarfed by the database. But it's also work that almost never changes between requests — the shape of an endpoint is fixed the moment you register it.

[Veloce](https://veloceframework.com) leans on that: it does the inspection **once at registration** and runs a compiled plan on every request after. This post is about how that works, and — importantly — what it does and doesn't buy you.

## The per-request cost in a typed framework

When a request arrives, a typical typed framework needs to answer, for this handler:

- which parameters come from the path, query, headers, cookies, or body,
- what each one's type is (and resolve string annotations under `from __future__ import annotations`),
- which dependencies (`Depends`) to resolve, in what order, with what caching,
- how to validate/coerce each value.

FastAPI answers these by interpreting a dependant tree (`solve_dependencies`) on each request. It's a clean design; it's also repeated work, since the answers are identical every time.

## Compile once, run a plan

Veloce moves that to registration. When you declare a route, it builds a `HandlerPlan` — a frozen IR describing exactly how to assemble the call:

- a flat list of parameter "slots" (kind, source, type, default, validation),
- the dependency sub-graph, grouped into parallel-safe waves and flagged for thread-offload where a dependency does blocking work.

(The declared response type isn't in the plan itself — it lives on the route's contract, `RouteInfo.response_model`, which the OpenAPI and MCP lowerings read from there.)

For eligible plans — parameter-only routes and many dependency graphs — it goes one step further: it **generates and `exec`s a straight-line resolver** specialized for that exact handler, with no per-request tree walk, `inspect`, or type-hint resolution. More complex plans (dependency waves, scopes and teardown, overrides, MCP context) run on a precomputed interpreter path instead — still built once at registration, just not code-generated.

```python
# Conceptually, registration turns this:
@app.get("/items/{item_id}")
async def get_item(item_id: int, q: str | None = None): ...

# into a compiled resolver that, per request, does roughly:
#   item_id = coerce_int(path["item_id"])
#   q = query.get("q")            # already knows the sources, types, defaults
#   return await get_item(item_id=item_id, q=q)
```

Reflection happened once. The hot path just runs the plan.

## The honest part: what this is *not*

It would be easy to slap a big multiplier on this. I won't, for two reasons.

**First, where the time actually goes.** In a real endpoint that touches a database or an external service, dispatch overhead is a rounding error. Compiling it away improves *dispatch-path headroom* — it does not make your app N times faster end-to-end. Anyone telling you a framework choice makes your DB-bound API several times faster is selling something.

**Second, the raw-speed ceiling.** Veloce's core is Python-first. A framework with a Cython core (BlackSheep) or a Rust core (Granian, Robyn) can beat it on the most trivial routes, where the language floor dominates. Reflection-free dispatch narrows the gap with other pure-Python frameworks; it doesn't repeal the GIL or the interpreter.

So rather than quote my own numbers, Veloce's entry is merged into the neutral [the-benchmarker](https://github.com/the-benchmarker/web-frameworks) suite ([PR #9455](https://github.com/the-benchmarker/web-frameworks/pull/9455)) — it will run alongside FastAPI, Starlette, Litestar and others under one harness, on hardware that isn't mine, with results in the next public run. That's the comparison worth trusting.

## Why bother, then?

Two reasons the compiled IR earns its place even though it won't 10x your DB-bound API:

1. **Consistency, not just speed.** The same `HandlerPlan` that drives dispatch also lowers to OpenAPI and to an MCP tool surface — so the documented contract, the enforced contract, and the agent-facing contract can't drift. The compilation is what makes that single-source guarantee cheap.
2. **Predictable hot path.** Because per-request dispatch work is fixed at registration rather than recomputed, it doesn't grow with traffic — which matters for the cases where the framework *is* on the critical path (high-RPS, small payloads, fan-out). How that lands against other frameworks is for the benchmark to say, not me.

## Try it

```bash
pip install veloceframework
```

Code and the `HandlerPlan` internals are at [github.com/Lokesh-Tallapaneni/veloce](https://github.com/Lokesh-Tallapaneni/veloce). If you dig into the resolver codegen and find a case where the compiled plan is wrong or slower than re-inspection, I want to hear about it — that's exactly the kind of issue that makes the design better.
