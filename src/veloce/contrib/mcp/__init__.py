"""Model Context Protocol integration — every Veloce route is also an AI tool.

The feature guide is `docs/guide/mcp.md`; this gateway only re-exports.
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
