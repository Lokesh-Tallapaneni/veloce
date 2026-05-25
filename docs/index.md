---
description: Veloce is an ultra-fast async Python web framework — ASGI-native, with radix routing, typed dependency injection, OpenAPI 3.1, WebSockets, and an in-memory test client.
hide:
  - navigation
  - toc
---

<div class="vl-hero" markdown>

<span class="vl-eyebrow">async · ASGI-native · typed</span>

# Veloce — Async Python Web Framework { .vl-hero__title }

<p class="vl-hero__tagline" markdown>
Fast, ergonomic async Python web framework. Routing, dependency
injection, OpenAPI, WebSockets, templating, sessions, and a built-in
test client — all in one tree.
</p>

[Get started](getting-started.md){ .md-button .md-button--primary }
[API reference](reference.md){ .md-button }

</div>

Veloce is an ASGI-native, async-first Python web framework for building APIs and full-stack applications. It draws Flask-compatible patterns (`g`, `flash`, blueprints, `@app.route`) and FastAPI-style typed dependency injection together into one tree — without depending on either. Requires Python 3.10+.

## Why Veloce

<div class="grid cards" markdown>

-   :material-lightning-bolt:{ .lg .middle } **Fast by design**

    ---

    Built on raw `asyncio` with `orjson` and `httptools`. Handler
    signatures are inspected once at registration — no reflection on
    the request hot path.

-   :material-sitemap:{ .lg .middle } **Radix-tree routing**

    ---

    Path parameters are matched and type-coerced during tree traversal,
    with built-in `int`, `float`, `uuid`, `path`, and custom converters.

-   :material-needle:{ .lg .middle } **Typed dependency injection**

    ---

    `Depends`, `Security`, and `SecurityScopes` resolve from precompiled
    plans, including `yield`-style dependencies with teardown.

-   :material-shield-check:{ .lg .middle } **Batteries included**

    ---

    OpenAPI 3.1, WebSockets, Server-Sent Events, background tasks,
    middleware, signed sessions, and signals — all in core.

-   :material-code-json:{ .lg .middle } **Validated, both ways**

    ---

    Pydantic v2 validates request bodies; `response_model` serialises
    and filters what goes back out.

-   :material-test-tube:{ .lg .middle } **Honest tests**

    ---

    The in-memory `TestClient` drives the real ASGI surface — middleware,
    encoding, and cookies included.

</div>

## A first look

```python
from veloce import Veloce, Request

app = Veloce()


@app.get("/hello/{name}")
async def hello(name: str):
    return {"hello": name}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
```

Type-annotated path parameters are coerced automatically, dictionaries
become JSON responses, and the route is registered on the radix tree at
import time.

[Read the getting-started guide :material-arrow-right:](getting-started.md){ .md-button .md-button--primary }
