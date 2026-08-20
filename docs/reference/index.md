---
description: Complete Veloce API reference - every public class, function, and decorator, grouped by topic and generated from the source docstrings.
---

# API Reference

Every name exported from the top-level `veloce` package, grouped by what it is for.
Each page is generated from the source docstrings.

- [Application](application.md) - The application object and its configuration.
- [Routers, Blueprints & Views](routers.md) - The route-group primitives a project is structured with.
- [Parameters & Converters](parameters.md) - The declarations that bind a handler's arguments to parts of the request, and the path converters behind them.
- [Requests](requests.md) - The request object and the parsed containers it exposes.
- [Responses](responses.md) - The base response and every response class shipped with the framework.
- [Dependency Injection](dependencies.md) - The markers that declare what a handler needs and how it is authorised.
- [Middleware](middleware.md) - The middleware base classes and every middleware shipped with the framework.
- [Security](security.md) - Authentication schemes, token handling, password hashing, and the signing primitives underneath them.
- [Sessions](sessions.md) - The session mapping and the server-side stores that back it.
- [Helpers & Context](helpers.md) - The request-scoped proxies, response shortcuts, and control-flow helpers.
- [Templating & Static Files](templating.md) - Jinja2 integration and the static-file mount.
- [WebSockets & Server-Sent Events](websockets.md) - The WebSocket connection object and the server-sent-event response types.
- [Background Work, Caching & Rate Limiting](tasks.md) - Work that runs after the response, the cache interface, rate-limit strategies, and runtime instrumentation.
- [Exceptions & Status Codes](exceptions.md) - The exception hierarchy, the built-in handlers, and the status-code constants.
- [OpenAPI & Encoding](openapi.md) - Schema generation and the JSON encoding layer.
- [Testing](testing.md) - The in-memory test clients.
- [MCP (AI Tools)](mcp.md) - The Model Context Protocol server, registries, and transports. Most applications drive MCP through `app.mount_mcp(...)`, `@app.mcp_tool`, and `@app.mcp_prompt`; these names are for callers that assemble or serve the registry themselves.
