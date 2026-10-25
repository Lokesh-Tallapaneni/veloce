"""MCP tool registry — the name -> tool table the server serves.

Assembled once at `mount_mcp` time from explicit `@app.mcp_tool(...)`
registrations plus every route flagged `expose_as_mcp_tool=True`.

The safety policy is **default-closed, not verb-based**: nothing is exposed
unless its author opts in.
An opted-in route is then exposed whatever its HTTP method is.
The one registration-time rule the registry enforces is `safety.py`'s: every
exposed handler must carry a non-empty description.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from veloce._handler_plan import build_plan
from veloce._model_backend import is_pydantic_model, resolve_return_model
from veloce._protocol_constants import ROUTE_METHOD_WEBSOCKET
from veloce.contrib.mcp._registry_base import Registry
from veloce.contrib.mcp.composition import mcp_mounts, renamed
from veloce.contrib.mcp.descriptors import MCPDescriptor
from veloce.contrib.mcp.icons import Icon, coerce_icons
from veloce.contrib.mcp.plan_bridge import (
    _plan_reaches_context,
    build_input_schema,
    build_output_schema,
)
from veloce.contrib.mcp.safety import require_mcp_description, validate_tool_annotations

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import HandlerPlan


# A `tools/list` visibility policy, sync or async: it narrows, and never widens.
ToolFilter = Callable[["MCPTool", Any], "bool | Awaitable[bool]"]


@dataclass(slots=True)
class MCPTool(MCPDescriptor):
    """One registered MCP tool."""

    handler: Callable[..., Any]
    plan: HandlerPlan
    input_schema: dict[str, Any]
    # Set by a tool that dispatches OTHER tools; a signature cannot show that.
    dispatches_tools: bool = False
    # Read by `_needs_stream`; `init=False` so no caller can claim streamlessness.
    may_stream: bool = field(default=True, init=False, repr=False, compare=False)
    # Route-level `dependencies=`, run before the handler's own `Depends` graph.
    route_dep_plans: list[Any] = field(default_factory=list)
    # The return runs through the route's response shaping, so excluded fields
    # never leak over MCP.
    route_info: Any = None
    # Bound onto the synthetic `Request.method`, so hooks see the route's real verb.
    route_method: str | None = None
    # Held to their declared type, not the query-string rules the resolver applies.
    dependency_inputs: dict[str, Any] = field(default_factory=dict)
    # Hints span the whole set, so `GET`+`DELETE` is flagged destructive.
    route_methods: list[str] = field(default_factory=list)
    # `None` when the result has no object schema; `tools/call` then returns text.
    output_schema: dict[str, Any] | None = None
    # Lets a pure `@app.mcp_tool` coerce its return to the advertised schema.
    output_model: Any = None
    # Checked before invocation; empty means no per-tool requirement.
    required_scopes: frozenset[str] = frozenset()
    # Read by a `mount_mcp(tool_filter=)` policy; the spec has no tag field to emit.
    tags: frozenset[str] = frozenset()
    # `True` lets a client run the call detached and poll `tasks/result`. Opt-in.
    task_support: bool = False
    # `readOnlyHint` and friends; `None` leaves the spec's cautious defaults.
    annotations: dict[str, Any] | None = None
    # An upstream result is relayed unchanged; re-shaping would lose `isError`.
    passthrough_result: bool = False
    # On a `derive_tool` tool: published surface -> original handler arguments.
    derived_from: Any = None
    # Every version of a name is registered; the highest answers a bare call.
    version: str | None = None
    # Ascending. `init=False`: meaningful only with the sibling map a copy lacks.
    version_history: tuple[str, ...] = field(default=(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # A property of the plan, so a `replace` copy re-derives against its own.
        self.may_stream = (
            self.dispatches_tools
            or _plan_reaches_context(self.plan)
            or any(_plan_reaches_context(dep) for dep in self.route_dep_plans)
        )


# The spec defines no version field, so publishing and naming one share this key.
VERSION_META_KEY = "veloce"


def _version_key(version: str | None) -> tuple[int, tuple[int, ...], int, str]:
    """Order two version labels, comparing dotted integers numerically.

    ``"10.0"`` follows ``"2.0"``, and ``"1.0.0-beta"`` sits below ``"1.0.0"`` so a
    call naming no version never reaches a prerelease. Suffixes compare textually,
    which is not semantic-versioning precedence; a label with no numeric stem is
    ordered as text below every numeric one.
    """
    if version is None:
        return (0, (), 0, "")
    stem, _, suffix = version.partition("-")
    parts = stem.split(".")
    if parts and all(part.isdigit() for part in parts):
        # A bare release outranks its own suffixed forms; `1` marks "no suffix".
        return (2, tuple(int(part) for part in parts), 0 if suffix else 1, suffix)
    return (1, (), 0, version)


@dataclass(slots=True)
class ToolRegistry(Registry[MCPTool]):
    """Name -> `MCPTool`, plus the shared JSON Schema component registry.

    `schemas` holds Pydantic-model components shared across tool input schemas
    and referenced by `$ref`, mirroring OpenAPI `components.schemas`.
    """

    tools: dict[str, MCPTool] = field(default_factory=dict)
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Only a name with more than one version has an entry, so most servers none.
    versions: dict[str, dict[str, MCPTool]] = field(default_factory=dict)

    @property
    def _store(self) -> dict[str, MCPTool]:
        return self.tools

    def _key(self, item: MCPTool) -> str:
        return item.name

    def _duplicate_message(self, key: str) -> str:
        return (
            f"Duplicate MCP tool name {key!r}. Tool names must be unique; "
            "rename the handler, pass name=, adjust the blueprint namespace, or "
            "give each registration its own version=."
        )

    def add(self, tool: MCPTool) -> None:
        """Register `tool`, rejecting a name already taken by the same version.

        Two registrations sharing a name under different versions are both kept:
        the higher is listed, and either answers a call naming its own.
        """
        existing = self.tools.get(tool.name)
        if existing is None or existing.version is None or tool.version is None:
            self.register(tool)
            return
        if tool.version == existing.version:
            raise ValueError(self._duplicate_message(tool.name))
        siblings = self.versions.setdefault(tool.name, {existing.version: existing})
        siblings[tool.version] = tool
        listed = max(siblings.values(), key=lambda candidate: _version_key(candidate.version))
        listed.version_history = tuple(sorted(siblings, key=_version_key))
        self.tools[tool.name] = listed

    def resolve(self, name: str, version: str | None) -> MCPTool | None:
        """Return the tool `name` registered under `version`, or the listed one."""
        if version is None:
            return self.get(name)
        siblings = self.versions.get(name)
        if siblings is None:
            listed = self.get(name)
            return listed if listed is not None and listed.version == version else None
        return siblings.get(version)


def _output_schema_for(
    handler: Callable[..., Any], route_info: Any, schemas_registry: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, Any]:
    """Build the tool's MCP output schema and the model it was derived from.

    A route `response_model` wins; otherwise the handler's Pydantic return type.
    A non-model output has no object schema and yields `(None, None)`.
    """
    model: Any = None
    # Alias usage must match the dump, so `structuredContent` matches `outputSchema`.
    by_alias = False
    if route_info is not None:
        response_model = route_info.response_model
        if is_pydantic_model(response_model):
            model = response_model
            by_alias = route_info.response_model_by_alias
    if model is None:
        model = resolve_return_model(handler)
    if model is None:
        return None, None
    schema = build_output_schema(model, schemas_registry, by_alias=by_alias)
    # Include/exclude makes presence conditional; the value omits, never adds.
    if (
        schema is not None
        and route_info is not None
        and (route_info.response_model_include or route_info.response_model_exclude)
    ):
        schema.pop("required", None)
    return schema, model


def _tool_name_from_route_name(route_name: str) -> str:
    """Derive a tool name from a route name, mapping the blueprint dot to ``_``.

    `<blueprint>.<handler>` becomes `<blueprint>_<handler>`, a single valid
    identifier the client can call.
    """
    return route_name.replace(".", "_")


def _register_explicit_tool(
    registry: ToolRegistry,
    handler: Callable[..., Any],
    *,
    name: str | None,
    description: str | None,
    namespace: str | None,
    scopes: frozenset[str] | None = None,
    tags: frozenset[str] | None = None,
    icons: Sequence[Icon] | None = None,
    task_support: bool = False,
    annotations: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    version: str | None = None,
) -> None:
    """Add an `@app.mcp_tool`-registered handler to `registry`."""
    base = name or handler.__name__
    tool_name = f"{namespace}_{base}" if namespace else base
    desc = require_mcp_description(tool_name, description)
    plan = build_plan(handler)
    dependency_inputs: dict[str, Any] = {}
    schema = build_input_schema(plan, registry.schemas, dependency_inputs=dependency_inputs)
    output_schema, output_model = _output_schema_for(handler, None, registry.schemas)
    registry.add(
        MCPTool(
            name=tool_name,
            description=desc,
            handler=handler,
            plan=plan,
            input_schema=schema,
            dependency_inputs=dependency_inputs,
            output_schema=output_schema,
            output_model=output_model,
            required_scopes=scopes or frozenset(),
            tags=tags or frozenset(),
            icons=coerce_icons(icons),
            task_support=task_support,
            annotations=validate_tool_annotations(annotations),
            meta=meta,
            version=version,
        )
    )


def _tool_from_route(
    info: Any, methods: list[str], schemas_registry: dict[str, dict[str, Any]]
) -> MCPTool:
    """Build the `MCPTool` for one exposed route.

    Shared with the resource registry, so both derive the name, schemas and
    route-replay fields one way. `methods` is the route's full verb set: its
    leading verb drives the synthetic request method, the whole set the hints.
    """
    tool_name = _tool_name_from_route_name(info.name)
    desc = require_mcp_description(tool_name, info.mcp_description)
    plan = (
        info.handler_plan
        if info.handler_plan is not None
        else build_plan(info.handler, path_params=frozenset(info.param_names))
    )
    dependency_inputs: dict[str, Any] = {}
    schema = build_input_schema(
        plan, schemas_registry, info.path_template, dependency_inputs=dependency_inputs
    )
    output_schema, output_model = _output_schema_for(info.handler, info, schemas_registry)
    return MCPTool(
        name=tool_name,
        description=desc,
        title=info.summary or None,
        handler=info.handler,
        plan=plan,
        input_schema=schema,
        dependency_inputs=dependency_inputs,
        meta=getattr(info, "mcp_meta", None),
        output_schema=output_schema,
        output_model=output_model,
        route_dep_plans=info.route_dep_plans,
        route_info=info,
        route_method=methods[0],
        route_methods=methods,
        required_scopes=info.mcp_scopes or frozenset(),
        tags=frozenset(getattr(info, "tags", None) or ()),
        icons=coerce_icons(getattr(info, "mcp_icons", None)),
        task_support=bool(getattr(info, "mcp_task_support", False)),
    )


def build_registry(app: Any) -> ToolRegistry:
    """Assemble the tool registry from explicit tools plus exposed routes."""
    registry = ToolRegistry()

    for tool in getattr(app, "_mcp_tools", ()):
        _register_explicit_tool(
            registry,
            tool.handler,
            name=tool.name,
            description=tool.description,
            namespace=tool.namespace,
            scopes=tool.scopes,
            tags=tool.tags,
            icons=tool.icons,
            task_support=tool.task_support,
            annotations=tool.annotations,
            meta=tool.meta,
            version=tool.version,
        )

    # Hidden routes are walked too; WebSocket routes have no tool shape. A
    # multi-verb route shares one `RouteInfo`, so group by identity, not handler.
    exposed: dict[int, Any] = {}
    methods_by_route: dict[int, list[str]] = {}
    for method, _path, info in app.iter_routes(include_hidden=True):
        if method == ROUTE_METHOD_WEBSOCKET or not info.expose_as_mcp_tool:
            continue
        route_id = id(info)
        if route_id not in exposed:
            exposed[route_id] = info
            methods_by_route[route_id] = []
        methods_by_route[route_id].append(method)

    for route_id, info in exposed.items():
        registry.add(_tool_from_route(info, methods_by_route[route_id], registry.schemas))

    for proxied in getattr(app, "_mcp_prebuilt_tools", ()):
        registry.add(proxied)

    # A sub-app's registry merges its own mounts first, so nesting composes.
    for namespace, sub_app in mcp_mounts(app):
        for tool in build_registry(sub_app).tools.values():
            registry.add(renamed(tool, namespace))

    return registry
