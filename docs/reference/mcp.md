---
description: Veloce API reference - mcp (ai tools).
---

# MCP (AI Tools)

The Model Context Protocol server, registries, and transports. Most applications drive MCP through `app.mount_mcp(...)`, `@app.mcp_tool`, and `@app.mcp_prompt`; these names are for callers that assemble or serve the registry themselves.

::: veloce.MCPContext
::: veloce.contrib.mcp.MCPSession

::: veloce.contrib.mcp.MCPServer
::: veloce.contrib.mcp.MCPTool
::: veloce.contrib.mcp.MCPResource
::: veloce.contrib.mcp.MCPPrompt
::: veloce.contrib.mcp.MCPTask
::: veloce.contrib.mcp.ToolRegistry
::: veloce.contrib.mcp.ResourceRegistry
::: veloce.contrib.mcp.PromptRegistry
::: veloce.contrib.mcp.TaskRegistry
The seam an out-of-tree spec area implements against: subclass `Capability`, annotate the handler map with `MethodHandler`, and pass an instance to `MCPServer(capabilities=[...])`.

::: veloce.contrib.mcp.Capability
::: veloce.contrib.mcp.MethodHandler
::: veloce.contrib.mcp.ToolsCapability
::: veloce.contrib.mcp.ResourcesCapability
::: veloce.contrib.mcp.PromptsCapability
::: veloce.contrib.mcp.LoggingCapability
::: veloce.contrib.mcp.TasksCapability
::: veloce.contrib.mcp.SubscriptionsCapability
::: veloce.contrib.mcp.CompletionsCapability
::: veloce.contrib.mcp.CompletionResult
::: veloce.contrib.mcp.ToolFilter
::: veloce.contrib.mcp.build_registry
::: veloce.contrib.mcp.build_resource_registry
::: veloce.contrib.mcp.build_prompt_registry

Composing a surface from more than one source: tools mounted from a sub-application, served from an upstream MCP server, or derived from a tool this application already registers.

::: veloce.contrib.mcp.add_mcp_proxy
::: veloce.contrib.mcp.derive_tool
::: veloce.contrib.mcp.ArgTransform

What a `sample_with_tools` run reports back to the handler that started it.

::: veloce.contrib.mcp.SamplingRun
::: veloce.contrib.mcp.SampledToolCall

The content a tool, resource, or prompt may return beyond plain text.

::: veloce.contrib.mcp.ContentBlock
::: veloce.contrib.mcp.TextContent
::: veloce.contrib.mcp.ImageContent
::: veloce.contrib.mcp.AudioContent
::: veloce.contrib.mcp.EmbeddedResource
::: veloce.contrib.mcp.ResourceLink
::: veloce.contrib.mcp.Icon

Transports, and the store that lets HTTP sessions outlive one worker.

::: veloce.contrib.mcp.Transport
::: veloce.contrib.mcp.BidirectionalTransport
::: veloce.contrib.mcp.register_http_transport
::: veloce.contrib.mcp.register_sse_transport
::: veloce.contrib.mcp.StdioTransport
::: veloce.contrib.mcp.serve_stdio
::: veloce.contrib.mcp.SessionBackend
::: veloce.contrib.mcp.SessionRecord

Authorization: validating a bearer token on the resource server, and issuing one from an authorization server of your own.

::: veloce.contrib.mcp.MCPAuth
::: veloce.contrib.mcp.MCPAuthorizationServer
::: veloce.contrib.mcp.register_authorization_server
::: veloce.contrib.mcp.AuthorizationStore
::: veloce.contrib.mcp.InMemoryAuthorizationStore
::: veloce.contrib.mcp.OAuthClient
::: veloce.contrib.mcp.AuthorizationCode
::: veloce.contrib.mcp.AccessToken

Errors. A handler raising one of these surfaces it to the client as the JSON-RPC error carrying its code; anything else a tool raises is reported in-band as an `isError` result.

::: veloce.contrib.mcp.MCPError
::: veloce.contrib.mcp.InvalidRequestError
::: veloce.contrib.mcp.MethodNotFoundError
::: veloce.contrib.mcp.InvalidParamsError
::: veloce.contrib.mcp.InternalError
::: veloce.contrib.mcp.ResourceNotFoundError
::: veloce.contrib.mcp.AuthorizationError
::: veloce.contrib.mcp.MCPCapabilityError
::: veloce.contrib.mcp.OriginNotAllowedError
::: veloce.contrib.mcp.ProtocolVersionError
::: veloce.contrib.mcp.HeaderMismatchError
::: veloce.contrib.mcp.SessionRequiredError
::: veloce.contrib.mcp.SessionNotFoundError
::: veloce.contrib.mcp.MCPRequestError

::: veloce.contrib.mcp.JSON_SCHEMA_DIALECT
