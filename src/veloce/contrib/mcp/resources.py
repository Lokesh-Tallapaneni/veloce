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
from urllib.parse import unquote

from veloce._protocol_constants import HTTP_METHOD_GET, HTTP_METHOD_HEAD, ROUTE_METHOD_WEBSOCKET
from veloce.contrib.mcp._registry_base import Registry
from veloce.contrib.mcp.composition import mcp_mounts
from veloce.contrib.mcp.descriptors import MCPDescriptor
from veloce.contrib.mcp.icons import coerce_icons
from veloce.contrib.mcp.registry import MCPTool, _tool_from_route

if TYPE_CHECKING:  # pragma: no cover
    from re import Pattern

# A resource is read-only data, so only a safe (non-mutating) verb may back one.
# A route carrying a mutating verb is a tool, never a resource.
_RESOURCE_METHODS = frozenset({HTTP_METHOD_GET, HTTP_METHOD_HEAD})

# An RFC 6570 variable inside a URI template, in either of the two forms the
# spec's templates use. `{name}` is simple expansion: the value is one URI
# segment, and a reserved character in it arrives percent-encoded. `{+name}` is
# reserved expansion: the value may carry reserved characters - `/` above all -
# literally, so it spans segments. A template variable names one route path
# parameter, so the name is a Python identifier.
_URI_TEMPLATE_VAR = re.compile(r"\{(\+?)([A-Za-z_][A-Za-z0-9_]*)\}")


# ── Descriptor and registry ───────────────────────────────


@dataclass(slots=True)
class MCPResource(MCPDescriptor):
    """One registered MCP resource (a read-only route addressed by URI)."""

    uri: str
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
    # How much of the template is literal text rather than variables. Two
    # templates can match the same URI - a catch-all `docs://{+path}` also matches
    # what `docs://{+path}/meta` was registered for - and the one spelling out
    # more of the URI is the one that meant it. Computed at build time so a read
    # ranks candidates without re-reading their templates.
    specificity: int = 0
    # Size in bytes a client may show before reading, and the annotations
    # (audience, priority) saying who the resource is for. Both are declared
    # rather than measured: a listing must not have to read every resource.
    size: int | None = None
    annotations: dict[str, Any] | None = None


@dataclass(slots=True)
class ResourceRegistry(Registry[MCPResource]):
    """URI -> `MCPResource`, plus the shared JSON Schema component registry."""

    resources: dict[str, MCPResource] = field(default_factory=dict)
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The template resources, most specific first. More than one template can
    # match a URI - a catch-all matches everything a longer one was registered
    # for - and the one spelling out more of the URI is the one that meant it.
    # Ranking here, at registration, lets a read stop at its first match instead
    # of scanning for a better one, and keeps statics out of that scan entirely.
    _ranked_templates: list[MCPResource] = field(default_factory=list)

    @property
    def _store(self) -> dict[str, MCPResource]:
        return self.resources

    # A resource is keyed by its URI, not its name (two resources may share a
    # tool name yet must expose distinct URIs).
    def _key(self, item: MCPResource) -> str:
        return item.uri

    def _duplicate_message(self, key: str) -> str:
        return (
            f"Duplicate MCP resource URI {key!r}. Resource URIs must be unique; "
            "give the route a distinct mcp_resource_uri."
        )

    def add(self, resource: MCPResource) -> None:
        """Register `resource`, rejecting a URI already taken."""
        self.register(resource)
        if resource.pattern is not None:
            self._ranked_templates.append(resource)
            self._ranked_templates.sort(key=lambda entry: -entry.specificity)

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
        # Ranked most-specific-first at registration, so the first template that
        # matches is the one that meant it and the scan stops there.
        for resource in self._ranked_templates:
            matched = resource.pattern.fullmatch(uri)  # type: ignore[union-attr]
            if matched is not None:
                return resource, _decode_values(matched.groupdict())
        return None


# ── URI templates ─────────────────────────────────────────


def _uri_template_vars(uri: str) -> list[str]:
    """Return the RFC 6570 variable names declared in a URI template."""
    return [name for _operator, name in _URI_TEMPLATE_VAR.findall(uri)]


def _compile_uri_template(uri: str) -> Pattern[str]:
    """Compile a URI template into a matcher capturing each variable's value.

    Literal spans are escaped; a ``{name}`` becomes a named group matching a
    single non-slash segment, the granularity a route path parameter occupies,
    and a ``{+name}`` one matching across segments so a whole path binds to one
    variable.
    """
    parts: list[str] = []
    last = 0
    for match in _URI_TEMPLATE_VAR.finditer(uri):
        parts.append(re.escape(uri[last : match.start()]))
        span = ".+" if match.group(1) else "[^/]+"
        parts.append(f"(?P<{match.group(2)}>{span})")
        last = match.end()
    parts.append(re.escape(uri[last:]))
    return re.compile("".join(parts))


def _template_specificity(uri: str) -> int:
    """Return how many characters of a template are literal rather than variable."""
    return len(uri) - sum(len(match.group(0)) for match in _URI_TEMPLATE_VAR.finditer(uri))


def _decode_values(values: dict[str, str]) -> dict[str, str]:
    """Percent-decode a matched template's values.

    A client percent-encodes a value to carry a character the URI syntax reserves,
    so the handler must receive what was meant - `a%2Fb.py`, not the escape. The
    `%` test keeps a value that was never encoded off `unquote` entirely, which is
    the usual case.
    """
    return {name: unquote(value) if "%" in value else value for name, value in values.items()}


# ── Building the registry ─────────────────────────────────


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
        specificity=_template_specificity(uri),
        title=info.summary or None,
        icons=coerce_icons(getattr(info, "mcp_icons", None)),
        meta=getattr(info, "mcp_meta", None),
        size=getattr(info, "mcp_resource_size", None),
        annotations=getattr(info, "mcp_resource_annotations", None),
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
    for method, _path, info in app.iter_routes(include_hidden=True):
        if method == ROUTE_METHOD_WEBSOCKET or not info.expose_as_mcp_resource:
            continue
        route_id = id(info)
        if route_id not in exposed:
            exposed[route_id] = info
            methods_by_route[route_id] = []
        methods_by_route[route_id].append(method)

    for route_id, info in exposed.items():
        registry.add(_resource_from_route(info, methods_by_route[route_id], registry.schemas))

    # A resource keeps its URI: that is the client-facing address of the thing,
    # not a name this server may rewrite. Two sub-apps publishing one URI is a
    # collision `add` reports.
    for _namespace, sub_app in mcp_mounts(app):
        for resource in build_resource_registry(sub_app).resources.values():
            registry.add(resource)

    return registry
