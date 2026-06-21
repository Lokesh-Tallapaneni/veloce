---
description: Complete Veloce API reference — every public class, function, and decorator, auto-generated from docstrings via mkdocstrings.
---

# API Reference

This page is generated from the docstrings of the public `veloce`
package — every name exported from the top-level namespace.

::: veloce
    options:
      show_root_heading: false
      show_root_toc_entry: false
      heading_level: 2

## Routing internals

Introspection types exposed from `veloce.routing` for advanced use (custom
dispatch, route inspection). They are not part of the top-level namespace.

::: veloce.routing.RouteInfo
::: veloce.routing.RouteMatch

## HTTP data structures

Additional parsed-header and value containers exposed from `veloce.http`.

::: veloce.http.QueryParams
::: veloce.http.CacheControl
::: veloce.http.HeaderSet
::: veloce.http.parse_multipart_form
::: veloce.http.header_key
::: veloce.http.header_get
::: veloce.http.header_present

## OpenAPI helpers

Lower-level OpenAPI helpers used by `app.openapi()` / the docs routes, exposed
from `veloce.contrib.openapi` for callers that build the schema directly.

::: veloce.contrib.openapi.get_openapi_schema
::: veloce.contrib.openapi.setup_openapi_routes

## MCP — advanced API

The Model Context Protocol server, tool/resource/prompt registries, and
transports exposed from `veloce.contrib.mcp`. Most applications drive MCP through
`app.mount_mcp(...)`, `@app.mcp_tool`, and `@app.mcp_prompt`; these names are for
callers that assemble or serve the registry themselves.

::: veloce.contrib.mcp.MCPServer
::: veloce.contrib.mcp.MCPTool
::: veloce.contrib.mcp.MCPResource
::: veloce.contrib.mcp.MCPPrompt
::: veloce.contrib.mcp.ToolRegistry
::: veloce.contrib.mcp.ResourceRegistry
::: veloce.contrib.mcp.PromptRegistry
::: veloce.contrib.mcp.build_registry
::: veloce.contrib.mcp.build_resource_registry
::: veloce.contrib.mcp.build_prompt_registry
::: veloce.contrib.mcp.register_http_transport
::: veloce.contrib.mcp.StdioTransport
::: veloce.contrib.mcp.serve_stdio
::: veloce.contrib.mcp.MCPError
::: veloce.contrib.mcp.InvalidRequestError
::: veloce.contrib.mcp.MethodNotFoundError
::: veloce.contrib.mcp.InvalidParamsError
::: veloce.contrib.mcp.InternalError
::: veloce.contrib.mcp.ResourceNotFoundError
::: veloce.contrib.mcp.AuthorizationError
