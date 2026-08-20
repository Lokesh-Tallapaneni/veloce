---
description: Veloce API reference - mcp (ai tools).
---

# MCP (AI Tools)

The Model Context Protocol server, registries, and transports. Most applications drive MCP through `app.mount_mcp(...)`, `@app.mcp_tool`, and `@app.mcp_prompt`; these names are for callers that assemble or serve the registry themselves.

::: veloce.MCPContext

::: veloce.contrib.mcp.MCPServer
::: veloce.contrib.mcp.MCPTool
::: veloce.contrib.mcp.MCPResource
::: veloce.contrib.mcp.MCPPrompt
::: veloce.contrib.mcp.MCPTask
::: veloce.contrib.mcp.ToolRegistry
::: veloce.contrib.mcp.ResourceRegistry
::: veloce.contrib.mcp.PromptRegistry
::: veloce.contrib.mcp.TaskRegistry
::: veloce.contrib.mcp.TasksCapability
::: veloce.contrib.mcp.SubscriptionsCapability
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
