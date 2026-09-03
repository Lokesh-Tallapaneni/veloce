---
description: Veloce framework (veloceframework) is an async Python web framework where one route definition drives an HTTP endpoint, an OpenAPI operation and an MCP tool. ASGI-native, typed dependency injection, WebSockets.
hide:
  - navigation
  - toc
---

<div class="vl-hero" markdown>

<span class="vl-eyebrow">async · ASGI-native · typed · MCP-native</span>

# Veloce — Async Python Web Framework { .vl-hero__title }

<p class="vl-hero__tagline" markdown>
One route definition is an HTTP endpoint, an OpenAPI operation and an
MCP tool. Routing, typed dependency injection, WebSockets, templating,
sessions and a built-in test client — all in one tree, no second service
to keep in sync.
</p>

[Get started](getting-started.md){ .md-button .md-button--primary }
[API reference](reference/index.md){ .md-button }

<p class="vl-hero__badges" markdown>
<span class="vl-badge">Python 3.10+</span>
<span class="vl-badge">MIT licensed</span>
<span class="vl-badge">MCP built in</span>
<span class="vl-badge">pip install veloceframework</span>
</p>

</div>

Veloce framework (PyPI: `veloceframework`) is an ASGI-native, async-first Python web framework for building APIs and full-stack applications. It draws Flask-compatible patterns (`g`, `flash`, blueprints, `@app.route`) and FastAPI-style typed dependency injection together into one tree — without depending on either. Requires Python 3.10+.

```python title="app.py"
from veloce import Veloce

app = Veloce()


@app.get("/hello/{name}")
async def hello(name: str):
    return {"hello": name}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
```

Type-annotated path parameters are coerced automatically, dictionaries become
JSON responses, and the route is registered on the radix tree at import time.

One flag also serves that handler to an AI agent as an
[MCP](guide/mcp.md) tool:

```python title="app.py" hl_lines="4"
from veloce import Veloce

app = Veloce()


@app.get("/users/{user_id}", expose_as_mcp_tool=True,
         mcp_description="Fetch a user by id")
async def get_user(user_id: int):
    return {"id": user_id}
```

There is no second service and no re-declared schema. An HTTP client calls the
endpoint, an agent calls the tool, and both run the same dependencies, the same
validation and the same `Security()` check — because they are two emissions of
one route contract, not two implementations. New here?
[Why Veloce Exists](surpass/why-veloce-exists.md) explains the one-IR
architecture the rest of the framework follows from.

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

[Follow the tutorial :material-arrow-right:](tutorial/index.md){ .md-button .md-button--primary }
