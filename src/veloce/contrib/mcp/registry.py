"""MCP tool registry - the name -> tool table the server serves.

A `ToolRegistry` is assembled once, at `mount_mcp` time, from two sources:

- explicit `@app.mcp_tool(...)` registrations (MCP-only tools), and
- a walk of the app's `RouteInfo` list, keeping every route flagged
  `expose_as_mcp_tool=True`.

Each entry carries the handler, its precompiled `HandlerPlan` (reused from
route registration, or built on demand for an MCP-only tool), the derived
input JSON Schema, and the LLM-facing description. The safety policy is
enforced here: a mutating route is never auto-exposed, and every exposed
handler must carry a non-empty description.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from veloce._handler_plan import build_plan
from veloce._protocol_constants import ROUTE_METHOD_WEBSOCKET
from veloce.contrib.mcp.plan_bridge import build_input_schema
from veloce.contrib.mcp.safety import is_safe_to_auto_expose, require_mcp_description

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import HandlerPlan


@dataclass(slots=True)
class MCPTool:
    """One registered MCP tool."""

    name: str
    description: str
    handler: Callable
    plan: HandlerPlan
    input_schema: dict[str, Any]
    # Route-level dependencies (`dependencies=[...]` on the route / router /
    # blueprint). These run before the handler's own `Depends` graph, exactly
    # as the HTTP and WebSocket dispatch paths run them, so a route-level guard
    # protects the agent-facing call too. Empty for `@app.mcp_tool` tools,
    # which have no route.
    route_dep_plans: list[Any] = field(default_factory=list)
    # The `RouteInfo` this tool was derived from, or `None` for a pure
    # `@app.mcp_tool`. When present the server runs the handler return through
    # the same response shaping the HTTP path applies for that route - the
    # route `response_model` filtering (so excluded fields never leak over MCP)
    # and `Response`/`JSONResponse` body extraction (so a returned response
    # object yields its decoded body, not an object repr).
    route_info: Any = None
    # The route's primary HTTP method (the first method entry the router walk
    # yielded for this `RouteInfo`). Bound onto the synthetic `Request.method`
    # so a handler / dependency / `before_request` hook that branches on
    # `request.method` sees the route's real verb, not the MCP origin. `None`
    # for a pure `@app.mcp_tool`, which keeps the synthetic MCP method.
    route_method: str | None = None


@dataclass(slots=True)
class ToolRegistry:
    """Name -> `MCPTool`, plus the shared JSON Schema component registry.

    `schemas` holds Pydantic-model components shared across tool input
    schemas (mirroring OpenAPI `components.schemas`); a tool input schema
    references them by `$ref`.
    """

    tools: dict[str, MCPTool] = field(default_factory=dict)
    schemas: dict[str, dict] = field(default_factory=dict)

    def add(self, tool: MCPTool) -> None:
        if tool.name in self.tools:
            raise ValueError(
                f"Duplicate MCP tool name {tool.name!r}. Tool names must be "
                "unique; rename the handler, pass name=, or adjust the "
                "blueprint namespace."
            )
        self.tools[tool.name] = tool

    def get(self, name: str) -> MCPTool | None:
        return self.tools.get(name)


def _tool_name_from_route_name(route_name: str) -> str:
    """Derive a tool name from a route name, mapping the blueprint dot to ``_``.

    A blueprint route is named `<blueprint>.<handler>`; the MCP tool name
    namespaces it as `<blueprint>_<handler>` so it is a single valid
    identifier the client can call.
    """
    return route_name.replace(".", "_")


def _register_explicit_tool(
    registry: ToolRegistry,
    handler: Callable,
    *,
    name: str | None,
    description: str | None,
    namespace: str | None,
) -> None:
    """Add an `@app.mcp_tool`-registered handler to `registry`."""
    base = name or handler.__name__
    tool_name = f"{namespace}_{base}" if namespace else base
    desc = require_mcp_description(tool_name, description)
    plan = build_plan(handler)
    schema = build_input_schema(plan, registry.schemas)
    registry.add(
        MCPTool(
            name=tool_name,
            description=desc,
            handler=handler,
            plan=plan,
            input_schema=schema,
        )
    )


def build_registry(app: Any) -> ToolRegistry:
    """Assemble the tool registry from explicit tools plus exposed routes."""
    registry = ToolRegistry()

    # Explicit @app.mcp_tool registrations, recorded on the app at decoration
    # time as `(handler, name, description, namespace)` tuples.
    for handler, name, description, namespace in getattr(app, "_mcp_tools", ()):
        _register_explicit_tool(
            registry, handler, name=name, description=description, namespace=namespace
        )

    # Routes flagged for exposure. Walk every route (including those hidden
    # from the OpenAPI schema) so an exposed-but-unlisted route still becomes
    # a tool; skip WebSocket routes, which have no request/response tool shape.
    #
    # A route declared with several methods (`methods=["GET", "POST"]`) shares a
    # single `RouteInfo` object across its method entries, so the walk yields it
    # once per method. Deduplicate by `RouteInfo` identity to expose that one
    # route a single time - never by the handler callable, which would silently
    # drop a function intentionally mounted as two distinct named routes (or on
    # two blueprints). Two distinct routes that derive the same tool name still
    # collide at `registry.add`, preserving duplicate-tool-name detection.
    seen_routes: set[int] = set()
    for method, _path, info in app._collect_all_routes(include_hidden=True):
        if method == ROUTE_METHOD_WEBSOCKET or not info.expose_as_mcp_tool:
            continue
        # The mutating-verb gate still applies per method, so a handler
        # reachable only via POST must opt in explicitly (it did, to be here).
        if not is_safe_to_auto_expose(method):
            # An exposed mutating route is allowed, but only because the
            # author set expose_as_mcp_tool=True; the gate exists to block
            # *auto*-exposure, which this is not. Continue to register it.
            pass
        route_id = id(info)
        if route_id in seen_routes:
            continue
        seen_routes.add(route_id)

        tool_name = _tool_name_from_route_name(info.name)
        desc = require_mcp_description(tool_name, info.mcp_description)
        plan = info.handler_plan if info.handler_plan is not None else build_plan(info.handler)
        schema = build_input_schema(plan, registry.schemas)
        registry.add(
            MCPTool(
                name=tool_name,
                description=desc,
                handler=info.handler,
                plan=plan,
                input_schema=schema,
                route_dep_plans=info.route_dep_plans,
                route_info=info,
                # `method` is the first method entry yielded for this route
                # (deduplicated above), so a multi-method route adopts its
                # leading verb as the synthetic request method.
                route_method=method,
            )
        )

    return registry
