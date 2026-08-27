"""Model Context Protocol integration — every Veloce route is also an AI tool.

`veloce.contrib.mcp` exposes a Veloce app's handlers as Model Context Protocol
tools so an AI agent can call them over JSON-RPC 2.0. Register MCP-only tools
with `@app.mcp_tool(...)`, opt an existing route in with
`expose_as_mcp_tool=True` / `mcp_description=...`, then serve over stdio with
`app.mount_mcp(transport="stdio")`.

**Transports.** stdio, Streamable HTTP, and the legacy SSE split-endpoint wire.
The server negotiates the protocol version with the client and answers ``ping``.

**Tools.** A tool definition carries HTTP-derived annotation hints (read-only /
idempotent / destructive / open-world) with the route summary as
``annotations.title``, a top-level ``title``, an ``inputSchema`` declaring the
JSON Schema 2020-12 dialect, and - where the result has a declared object shape
- an ``outputSchema`` (also dialect-declared) whose structured value
``tools/call`` returns alongside the text block. A tool returning an image or
audio response emits the matching typed content block, and a route may return
its result as a ``resource_link`` or embedded ``resource`` block through the
``X-MCP-Resource-Link`` / ``X-MCP-Embedded-Resource`` response header.

**Resources and prompts.** A read-only route flagged
``expose_as_mcp_resource=True`` is served as a resource (``resources/list``,
``resources/templates/list``, ``resources/read``); a ``@app.mcp_prompt``
callable is served as a prompt template (``prompts/list``, ``prompts/get``); and
a ``@app.mcp_completer`` callable suggests values for a prompt or
resource-template argument (``completion/complete``). Any of the three may carry
opt-in ``icons`` (`Icon` objects) a client renders beside it.

**Tasks.** A tool flagged ``task_support=True`` may be invoked as a background
task: a task-augmented ``tools/call`` returns a ``CreateTaskResult`` the client
polls via ``tasks/get`` / ``tasks/result`` (``tasks/list`` / ``tasks/cancel``
round it out). The same handler runs whether the call is synchronous or a task.

**Sessions.** One `MCPSession` tracks the connection: it records the client's
advertised capabilities from ``initialize`` and rejects any request other than
``initialize`` / ``ping`` that precedes initialization. The ``initialize`` result
carries ``instructions`` (the app description / summary) and a
``serverInfo.title`` (the app title).

**Subscriptions.** With ``MCP_RESOURCE_SUBSCRIPTIONS`` enabled a client may
``resources/subscribe`` to a resource URI, and the app signals a change with
``MCPServer.notify_resource_updated`` (or ``notify_resources_list_changed``),
fanning ``notifications/resources/updated`` and
``notifications/resources/list_changed`` out to subscribed connections.

**Server-initiated requests.** Over a bidirectional transport a tool's
`MCPContext` issues ``ctx.sample`` (``sampling/createMessage``, with model
preferences and sampling tools), ``ctx.elicit`` (``elicitation/create``, in form
or URL mode) and ``ctx.roots`` (``roots/list``) - each gated on the client having
advertised the matching capability in ``initialize``.

**Also.** Tool search, versioning, proxying to an upstream MCP server, and an
OAuth 2.1 authorization server.
"""

from __future__ import annotations

from veloce.contrib.mcp.auth import MCPAuth
from veloce.contrib.mcp.authorization import (
    AccessToken,
    AuthorizationCode,
    AuthorizationStore,
    InMemoryAuthorizationStore,
    MCPAuthorizationServer,
    OAuthClient,
    register_authorization_server,
)
from veloce.contrib.mcp.capabilities import (
    Capability,
    LoggingCapability,
    PromptsCapability,
    ResourcesCapability,
    ToolsCapability,
)
from veloce.contrib.mcp.completion import CompletionResult, CompletionsCapability
from veloce.contrib.mcp.content import (
    AudioContent,
    ContentBlock,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
)
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.mcp.errors import (
    AuthorizationError,
    HeaderMismatchError,
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    MCPCapabilityError,
    MCPError,
    MethodNotFoundError,
    OriginNotAllowedError,
    ProtocolVersionError,
    ResourceNotFoundError,
    SessionNotFoundError,
    SessionRequiredError,
)
from veloce.contrib.mcp.icons import Icon
from veloce.contrib.mcp.plan_bridge import JSON_SCHEMA_DIALECT
from veloce.contrib.mcp.prompts import MCPPrompt, PromptRegistry, build_prompt_registry
from veloce.contrib.mcp.proxy import add_mcp_proxy
from veloce.contrib.mcp.registry import MCPTool, ToolFilter, ToolRegistry, build_registry
from veloce.contrib.mcp.resources import (
    MCPResource,
    ResourceRegistry,
    build_resource_registry,
)
from veloce.contrib.mcp.sampling import SampledToolCall, SamplingRun
from veloce.contrib.mcp.server import MCPServer, MethodHandler
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.subscriptions import SubscriptionsCapability
from veloce.contrib.mcp.tasks import MCPTask, TaskRegistry, TasksCapability
from veloce.contrib.mcp.transform import ArgTransform, derive_tool
from veloce.contrib.mcp.transports.base import BidirectionalTransport, Transport
from veloce.contrib.mcp.transports.http import register_http_transport
from veloce.contrib.mcp.transports.session_store import SessionBackend, SessionRecord
from veloce.contrib.mcp.transports.sse import register_sse_transport
from veloce.contrib.mcp.transports.stdio import MCPRequestError, StdioTransport, serve_stdio

__all__ = [
    # Server, registries and the per-call context
    "MCPContext",
    "MCPPrompt",
    "MCPResource",
    "MCPServer",
    "MCPSession",
    "MCPTool",
    "MethodHandler",
    "PromptRegistry",
    "ResourceRegistry",
    "ToolFilter",
    "ToolRegistry",
    "build_prompt_registry",
    "build_registry",
    "build_resource_registry",
    # Content blocks
    "AudioContent",
    "ContentBlock",
    "EmbeddedResource",
    "Icon",
    "ImageContent",
    "ResourceLink",
    "TextContent",
    # Capabilities
    "Capability",
    "CompletionResult",
    "CompletionsCapability",
    "LoggingCapability",
    "MCPTask",
    "PromptsCapability",
    "ResourcesCapability",
    "SubscriptionsCapability",
    "TaskRegistry",
    "TasksCapability",
    "ToolsCapability",
    # Sampling and tool derivation
    "ArgTransform",
    "SampledToolCall",
    "SamplingRun",
    "derive_tool",
    # Transports
    "BidirectionalTransport",
    "SessionBackend",
    "SessionRecord",
    "StdioTransport",
    "Transport",
    "add_mcp_proxy",
    "register_http_transport",
    "register_sse_transport",
    "serve_stdio",
    # Authentication and authorization
    "AccessToken",
    "AuthorizationCode",
    "AuthorizationStore",
    "InMemoryAuthorizationStore",
    "MCPAuth",
    "MCPAuthorizationServer",
    "OAuthClient",
    "register_authorization_server",
    # Errors
    "AuthorizationError",
    "HeaderMismatchError",
    "InternalError",
    "InvalidParamsError",
    "InvalidRequestError",
    "MCPCapabilityError",
    "MCPError",
    "MCPRequestError",
    "MethodNotFoundError",
    "OriginNotAllowedError",
    "ProtocolVersionError",
    "ResourceNotFoundError",
    "SessionNotFoundError",
    "SessionRequiredError",
    # Schema
    "JSON_SCHEMA_DIALECT",
]
