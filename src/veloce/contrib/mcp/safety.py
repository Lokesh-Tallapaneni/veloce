"""MCP safety policy - which routes may become tools and what they must declare.

Two rules, both enforced at registry-build time (registration), never on the
call path:

- A route bound to a mutating HTTP verb (POST / PUT / DELETE / PATCH) is
  never auto-exposed. Exposing it requires an explicit
  ``expose_as_mcp_tool=True`` on the route, so an AI agent cannot reach a
  state-changing endpoint the author did not deliberately open up.
- Every MCP-exposed handler must carry a non-empty ``mcp_description`` - the
  LLM-facing description, kept separate from the docstring. A missing or
  blank description raises at registration so the gap is caught before the
  server ever starts.
"""

from __future__ import annotations

# Verbs that change server state. A route on one of these is only ever
# exposed when the author passes `expose_as_mcp_tool=True` explicitly.
MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def is_safe_to_auto_expose(method: str) -> bool:
    """Whether `method` may be auto-exposed without an explicit opt-in."""
    return method.upper() not in MUTATING_METHODS


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
