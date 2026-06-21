"""MCP capability objects — the seam the server composes its spec areas from."""

from __future__ import annotations

from veloce.contrib.mcp.capabilities.base import Capability
from veloce.contrib.mcp.capabilities.concrete import (
    LoggingCapability,
    PromptsCapability,
    ResourcesCapability,
    ToolsCapability,
)

__all__ = [
    "Capability",
    "LoggingCapability",
    "PromptsCapability",
    "ResourcesCapability",
    "ToolsCapability",
]
