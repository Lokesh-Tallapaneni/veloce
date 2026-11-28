"""Slot classification — which plan slots are client input and which are injected."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veloce._handler_plan import (
    K_BODY_MODEL,
    K_DEPENDS,
    K_PARAM_MARKER,
    K_QUERY,
    K_QUERY_LIST,
)
from veloce.contrib.mcp.context import MCPContext

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import _Slot


# Slot kinds an agent supplies as JSON; a required one absent from `arguments`
# is a binding error, not a handler error.
_INPUT_KINDS = frozenset({K_BODY_MODEL, K_QUERY_LIST, K_PARAM_MARKER, K_QUERY})

# Keywords that annotate rather than constrain, so they stay outside the `anyOf`.
_ANNOTATION_KEYWORDS = frozenset({"description", "title", "default"})


# ── Reading the handler plan ──────────────────────────────
def _is_context_slot(slot: _Slot) -> bool:
    """Whether `slot` binds the MCPContext rather than an agent input.

    Detected by the parameter's TYPE annotation, never by name: a parameter merely
    *named* `ctx` but typed as a normal value stays an agent input. A parameter
    typed `MCPContext` lands on a K_QUERY slot - it is neither `Request` nor a
    model - so the K_QUERY guard scopes the check.
    """
    return slot.kind == K_QUERY and slot.target_type is MCPContext


def _plan_reaches_context(plan: Any, _seen: set[int] | None = None) -> bool:
    """Whether this plan, or anything it depends on, binds the `MCPContext`.

    Every extra message a `tools/call` can emit is reached through `MCPContext`,
    so a graph that never binds it produces exactly one response. This reads a
    signature, so a handler dispatching to another is `MCPTool.dispatches_tools`
    instead; an unwalkable plan answers `True` and keeps the streaming response.
    """
    if plan is None:
        return True
    slots = getattr(plan, "slots", None)
    if slots is None:
        return True
    _seen = set() if _seen is None else _seen
    if id(plan) in _seen:
        return False
    _seen.add(id(plan))
    for slot in slots:
        if _is_context_slot(slot):
            return True
        if slot.kind == K_DEPENDS and _plan_reaches_context(slot.sub_plan, _seen):
            return True
    return False
