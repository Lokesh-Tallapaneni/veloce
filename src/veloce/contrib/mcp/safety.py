"""MCP safety policy - what an exposed tool must declare.

Exposure itself is default-closed and explicit: a route becomes a tool only
when its author passes ``expose_as_mcp_tool=True`` (regardless of HTTP verb),
and an exposed route keeps its Security / Depends / middleware guards on the
agent-facing call. There is no auto-exposure, so no per-verb gate is needed.

This module enforces the one remaining registration-time rule: every
MCP-exposed handler must carry a non-empty ``mcp_description`` - the LLM-facing
description, kept separate from the docstring. A missing or blank description
raises at registry-build time, never on the call path, so the gap is caught
before the server ever starts.
"""

from __future__ import annotations


def require_mcp_description(tool_name: str, description: str | None) -> str:
    """Return a validated non-empty `mcp_description`, or raise.

    Enforced at registry-build time so a tool can never reach an agent
    without an LLM-facing description.
    """
    if description is None or not description.strip():
        raise ValueError(
            f"MCP tool {tool_name!r} is missing a description. "
            "Pass a non-empty mcp_description (for an exposed route) or "
            "description= (for @app.mcp_tool); it is the LLM-facing text "
            "and is required, separate from the handler docstring."
        )
    return description
