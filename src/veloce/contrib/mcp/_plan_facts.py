"""MCP plan facts — the "mcp" plan-extension provider for HandlerPlan.

Computes, from a handler's signature, the one fact MCP's own tool-schema
builder needs and nothing else does: whether the handler takes a variadic
(`*args`/`**kwargs`) parameter, which a JSON-RPC `arguments` object cannot
satisfy.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from veloce._handler_plan import HandlerPlan, register_plan_extension


@dataclass(slots=True, frozen=True)
class _McpPlanFacts:
    variadic_param: tuple[str, Any] | None


def _compute_mcp_plan_facts(sig: inspect.Signature, plan: HandlerPlan) -> _McpPlanFacts:
    for name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            return _McpPlanFacts(variadic_param=(name, param.kind))
    return _McpPlanFacts(variadic_param=None)


register_plan_extension("mcp", _compute_mcp_plan_facts)
