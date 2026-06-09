"""MCP resource registry — read-only routes exposed as Model Context Protocol resources.

A resource is the MCP primitive for data an agent reads by URI (the counterpart
to a tool, which it calls). Veloce maps a resource onto a read-only (`GET`/`HEAD`)
route flagged ``expose_as_mcp_resource=True`` with an ``mcp_resource_uri``: a
static URI for a route with no path parameters, or an RFC 6570 URI template
(``users://{user_id}``) whose variables bind the route's path parameters.

The registry is assembled once, at ``mount_mcp`` time, by walking the app's
routes. Each resource wraps the same `MCPTool` a tool exposure would build
(`registry._tool_from_route`), so a ``resources/read`` replays the full request
lifecycle through the shared invocation path - the route's `Depends`, `Security`,
middleware, and `response_model` all run exactly as on the HTTP and tool paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from veloce._protocol_constants import ROUTE_METHOD_WEBSOCKET
from veloce.contrib.mcp.registry import MCPTool, _tool_from_route

if TYPE_CHECKING:  # pragma: no cover
    from re import Pattern

# A resource is read-only data, so only a safe (non-mutating) verb may back one.
# A route carrying a mutating verb is a tool, never a resource.
_RESOURCE_METHODS = frozenset({"GET", "HEAD"})

# RFC 6570 simple variable (`{name}`) inside a URI template. Each variable maps
# to one route path parameter and matches a single non-slash URI segment.
_URI_TEMPLATE_VAR = re.compile(r"\{([^}]+)\}")


@dataclass(slots=True)
class MCPResource:
    """One registered MCP resource (a read-only route addressed by URI)."""

    uri: str
    name: str
    description: str
    tool: MCPTool
    # True when `uri` is a URI template (carries `{var}` placeholders bound to
    # the route's path parameters); such a resource is advertised through
    # ``resources/templates/list`` rather than ``resources/list``.
    is_template: bool
    # Compiled matcher for a template URI (`None` for a static resource); a
    # concrete ``resources/read`` URI is matched against it to recover the path
    # parameter values.
    pattern: Pattern[str] | None
    # The template variable names, in declaration order (empty for a static
    # resource). Each names a route path parameter.
    uri_param_names: tuple[str, ...]
    title: str | None = None


@dataclass(slots=True)
class ResourceRegistry:
    """URI -> `MCPResource`, plus the shared JSON Schema component registry."""

    resources: dict[str, MCPResource] = field(default_factory=dict)
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, resource: MCPResource) -> None:
        if resource.uri in self.resources:
            raise ValueError(
                f"Duplicate MCP resource URI {resource.uri!r}. Resource URIs must "
                "be unique; give the route a distinct mcp_resource_uri."
            )
        self.resources[resource.uri] = resource

    def statics(self) -> list[MCPResource]:
        """Return the concrete-URI resources (for ``resources/list``)."""
        return [r for r in self.resources.values() if not r.is_template]

    def templates(self) -> list[MCPResource]:
        """Return the URI-template resources (for ``resources/templates/list``)."""
        return [r for r in self.resources.values() if r.is_template]

    def match(self, uri: str) -> tuple[MCPResource, dict[str, str]] | None:
        """Resolve a concrete URI to its resource and extracted path parameters.

        A static resource matches by exact URI (no parameters); a template
        resource matches by its compiled pattern, yielding the path-parameter
        values to invoke the route with. Static resources are tried first so a
        concrete URI never falls through to a template that would also match it.
        """
        static = self.resources.get(uri)
        if static is not None and not static.is_template:
            return static, {}
        for resource in self.resources.values():
            if resource.pattern is not None:
                matched = resource.pattern.fullmatch(uri)
                if matched is not None:
                    return resource, matched.groupdict()
        return None


def _uri_template_vars(uri: str) -> list[str]:
    """Return the RFC 6570 variable names declared in a URI template."""
    return _URI_TEMPLATE_VAR.findall(uri)


def _compile_uri_template(uri: str) -> Pattern[str]:
    """Compile a URI template into a matcher capturing each variable's value.

    Literal spans are escaped; each ``{name}`` becomes a named group matching a
    single non-slash segment, the granularity a route path parameter occupies.
    """
    parts: list[str] = []
    last = 0
    for match in _URI_TEMPLATE_VAR.finditer(uri):
        parts.append(re.escape(uri[last : match.start()]))
        parts.append(f"(?P<{match.group(1)}>[^/]+)")
        last = match.end()
    parts.append(re.escape(uri[last:]))
    return re.compile("".join(parts))


def _resource_from_route(
    info: Any, methods: list[str], schemas_registry: dict[str, dict[str, Any]]
) -> MCPResource:
    """Build the `MCPResource` for one route flagged `expose_as_mcp_resource`."""
    verbs = {method.upper() for method in methods}
    if not verbs <= _RESOURCE_METHODS:
        raise ValueError(
            f"Route {info.name!r} is exposed as an MCP resource but serves "
            f"{sorted(verbs)}; a resource must be read-only (GET/HEAD). Expose a "
            "mutating route as a tool (expose_as_mcp_tool=True) instead."
        )

    uri = info.mcp_resource_uri
    if not uri or not uri.strip():
        raise ValueError(
            f"Route {info.name!r} is exposed as an MCP resource but has no "
            "mcp_resource_uri. Pass a URI (e.g. 'config://app') or a URI template "
            "('users://{user_id}') whose variables bind its path parameters."
        )

    template_vars = _uri_template_vars(uri)
    param_names = tuple(info.param_names)
    if set(template_vars) != set(param_names):
        raise ValueError(
            f"Route {info.name!r} mcp_resource_uri {uri!r} variables "
            f"{sorted(set(template_vars))} must match its path parameters "
            f"{sorted(set(param_names))} exactly. A static route takes a URI with "
            "no variables; a parameterised route takes one variable per path "
            "parameter."
        )

    is_template = bool(template_vars)
    tool = _tool_from_route(info, methods, schemas_registry)
    return MCPResource(
        uri=uri,
        name=tool.name,
        description=tool.description,
        tool=tool,
        is_template=is_template,
        pattern=_compile_uri_template(uri) if is_template else None,
        uri_param_names=tuple(template_vars),
        title=info.summary or None,
    )


def build_resource_registry(app: Any) -> ResourceRegistry:
    """Assemble the resource registry from routes flagged `expose_as_mcp_resource`.

    Mirrors the tool registry walk: every route is visited (including those
    hidden from the OpenAPI schema), WebSocket routes are skipped, and a
    multi-verb route is deduplicated by `RouteInfo` identity so it is exposed
    once with its full verb set.
    """
    registry = ResourceRegistry()
    exposed: dict[int, Any] = {}
    methods_by_route: dict[int, list[str]] = {}
    for method, _path, info in app._collect_all_routes(include_hidden=True):
        if method == ROUTE_METHOD_WEBSOCKET or not info.expose_as_mcp_resource:
            continue
        route_id = id(info)
        if route_id not in exposed:
            exposed[route_id] = info
            methods_by_route[route_id] = []
        methods_by_route[route_id].append(method)

    for route_id, info in exposed.items():
        registry.add(_resource_from_route(info, methods_by_route[route_id], registry.schemas))

    return registry
