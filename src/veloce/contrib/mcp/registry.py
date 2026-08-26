"""MCP tool registry — the name -> tool table the server serves.

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
from veloce.contrib.mcp.plan_bridge import build_input_schema, build_output_schema
from veloce.contrib.mcp.safety import require_mcp_description, validate_tool_annotations

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import HandlerPlan


# A `tools/list` visibility policy: given a registered tool and the request
# principal, return whether that caller may see it. Sync or async; a sync policy is
# offloaded so a blocking permission lookup cannot stall the event loop. It narrows
# the scope-checked set and can never widen it.
ToolFilter = Callable[["MCPTool", Any], "bool | Awaitable[bool]"]


@dataclass(slots=True)
class MCPTool(MCPDescriptor):
    """One registered MCP tool."""

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
    # The published inputs declared inside a `Depends` sub-dependency, by name.
    # `bind_arguments` coerces its own top-level slots but seeds these onto the
    # synthetic request for the HTTP resolver to read, and that resolver reads a
    # query string - where "1" and "yes" have to mean true. An agent sends typed
    # JSON, and this tool's own `inputSchema` says so, so the door holds them to
    # the declared type rather than to query-string rules.
    dependency_inputs: dict[str, Any] = field(default_factory=dict)
    # Every HTTP method the route serves (a multi-verb route yields several).
    # The annotation hints are computed conservatively across this whole set so
    # a `GET`+`DELETE` route is flagged destructive even though its leading verb
    # is `GET`. Empty for a pure `@app.mcp_tool`, which has no HTTP verb.
    route_methods: list[str] = field(default_factory=list)
    # The MCP `outputSchema`: a standalone JSON Schema for the structured result,
    # derived from the route `response_model` (or, failing that, the handler's
    # Pydantic return type). `None` when the result has no object schema (a
    # scalar / list return, or an untyped handler), in which case `tools/call`
    # returns only the text content block.
    output_schema: dict[str, Any] | None = None
    # The Pydantic model the `output_schema` was derived from, kept so a pure
    # `@app.mcp_tool` (which has no route `response_model` to re-filter through)
    # can validate / coerce its raw return into a value that conforms to the
    # advertised schema before it is emitted as `structuredContent`. `None`
    # whenever `output_schema` is `None`.
    output_model: Any = None
    # MCP authorization scopes the request principal must hold to invoke this
    # tool / read this resource. Empty means no per-tool requirement; a non-empty
    # set is checked before invocation and a miss yields an authorization error.
    required_scopes: frozenset[str] = frozenset()
    # Server-side labels for grouping tools, read by a `mount_mcp(tool_filter=)`
    # policy. A route-backed tool inherits its route's `tags`. Not advertised in
    # `tools/list`: the spec defines no tag field on a tool, so publishing one
    # would invent wire data a client cannot interpret.
    tags: frozenset[str] = frozenset()
    # Whether this tool may be invoked as a background task (task-augmented
    # `tools/call`). `False` (the default) advertises no task support and rejects
    # a task-augmented call; `True` lets a client run the call detached and poll
    # it via `tasks/get` / `tasks/result`. Opt-in per tool so the synchronous
    # path stays unchanged for every tool that does not ask for it.
    task_support: bool = False
    # Behaviour hints the author declared (`readOnlyHint` and friends). A
    # route-backed tool derives these from its HTTP verb; a tool with no route has
    # no verb to read from, so it either says what it does or says nothing. `None`
    # leaves the spec's own defaults in force, which assume the cautious reading.
    annotations: dict[str, Any] | None = None
    # True for a tool that forwards to another MCP server. Its handler returns the
    # upstream's own result object, which is relayed unchanged - re-shaping it
    # would nest a complete result inside a text block and lose the `isError` and
    # `structuredContent` the upstream reported.
    passthrough_result: bool = False
    # Set on a tool built by `derive_tool`: the argument translation that maps the
    # published surface back to what the original handler takes. `None` for a tool
    # whose arguments are its handler's own.
    derived_from: Any = None
    # The label this registration was published under, when the author declared
    # one. Tools sharing a name and differing in version are all registered; the
    # highest is the one listed and the one a call without a version reaches.
    version: str | None = None
    # Every version registered under this tool's name, ascending. Set on the
    # listed tool as its siblings arrive; empty for an unversioned tool.
    #
    # Excluded from `__init__` for the same reason `listing_entry` is, and with a
    # sharper consequence: it is only meaningful alongside the registry's own
    # sibling map, which a copy does not inherit. `dataclasses.replace` skips an
    # `init=False` field, so a tool copied by `derive_tool`, by a namespaced
    # mount or by anything else starts with an empty history rather than
    # advertising versions the copy's registry cannot serve.
    version_history: tuple[str, ...] = field(default=(), init=False, repr=False, compare=False)


# Where a tool's version is published in its `_meta`, and where a call names the
# version it wants. The spec defines no version field, so both live under one
# framework-namespaced key rather than inventing wire fields a client would not
# recognise. It lives here, beside the versioning it names, because every site
# that copies or relays a tool has to know not to carry it across.
VERSION_META_KEY = "veloce"


def _version_key(version: str | None) -> tuple[int, tuple[int, ...], int, str]:
    """Order two version labels, comparing dotted integers numerically.

    ``"10.0"`` follows ``"2.0"``, which a plain string sort gets backwards. A
    label carrying a suffix precedes the release it is a suffix of, so
    ``"1.0.0-beta"`` sits below ``"1.0.0"`` rather than above every number - a
    prerelease is not what a call naming no version should reach. Among suffixes
    the comparison is textual, which happens to order ``alpha`` before ``beta``
    before ``rc`` but is not semantic-versioning precedence and does not claim to
    be. A label with no numeric stem at all is ordered as text, below every
    numeric one, so an ordering exists whatever the author chose to write.
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

    `schemas` holds Pydantic-model components shared across tool input
    schemas (mirroring OpenAPI `components.schemas`); a tool input schema
    references them by `$ref`.
    """

    tools: dict[str, MCPTool] = field(default_factory=dict)
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    # name -> version -> tool, for names registered under more than one version.
    # Only such a name has an entry, so an unversioned server carries none.
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

        Two registrations sharing a name and declaring different versions are
        both kept: the higher one is listed and answers a call naming no
        version, and either answers a call naming its own.
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
    handler: Callable, route_info: Any, schemas_registry: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, Any]:
    """Build the tool's MCP output schema and the model it was derived from.

    A route `response_model` is the authoritative output contract and wins; a
    pure `@app.mcp_tool` (or a route with no `response_model`) falls back to the
    handler's Pydantic return type. A non-model output (scalar, list, untyped)
    has no object schema and yields `(None, None)`. The model is returned
    alongside the schema so a pure tool can validate its raw return against it.
    """
    model: Any = None
    # The schema's alias usage must match how the structured value is dumped: a
    # route `response_model` is dumped with the route's `response_model_by_alias`
    # (default False), while a return-type model and a pure tool dump with field
    # names. Build the schema with the matching `by_alias` so `structuredContent`
    # conforms to the advertised `outputSchema`.
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
    # `response_model_include` / `response_model_exclude` make field presence
    # conditional, so the bare model's `required` no longer matches the dumped
    # value (an excluded required field is absent). Drop `required` so a partial
    # object still conforms to the advertised schema; the value only ever omits
    # fields, never adds undeclared ones, so the looser schema stays sound.
    if (
        schema is not None
        and route_info is not None
        and (route_info.response_model_include or route_info.response_model_exclude)
    ):
        schema.pop("required", None)
    return schema, model


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

    Shared by the tool registry walk and the resource registry (a resource is a
    read-only route invoked through the same handler-replay path as a tool), so
    both derive the tool name, description, input/output schema, and the
    route-replay fields (`route_info`, `route_method`, `route_dep_plans`) one
    way. `methods` is the route's full verb set (a multi-verb route shares one
    `RouteInfo`); its leading verb drives the synthetic request method and the
    whole set drives the conservative annotation hints.
    """
    tool_name = _tool_name_from_route_name(info.name)
    desc = require_mcp_description(tool_name, info.mcp_description)
    plan = info.handler_plan if info.handler_plan is not None else build_plan(info.handler)
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

    # Explicit @app.mcp_tool registrations, recorded on the app at decoration
    # time as `(handler, name, description, namespace, scopes, tags, icons,
    # task_support, annotations, meta, version)` tuples.
    for (
        handler,
        name,
        description,
        namespace,
        scopes,
        tags,
        icons,
        task_support,
        declared_annotations,
        declared_meta,
        version,
    ) in getattr(app, "_mcp_tools", ()):
        _register_explicit_tool(
            registry,
            handler,
            name=name,
            description=description,
            namespace=namespace,
            scopes=scopes,
            tags=tags,
            icons=icons,
            task_support=task_support,
            annotations=declared_annotations,
            meta=declared_meta,
            version=version,
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
    # Group the yielded methods by `RouteInfo` identity, preserving first-seen
    # order, so the route is exposed once: its leading verb drives the synthetic
    # request method and its full verb set drives the conservative annotation
    # hints. Dedup by identity, never by handler callable - that would drop a
    # function intentionally mounted as two distinct named routes (or on two
    # blueprints). Exposure stays default-closed (`expose_as_mcp_tool=True`).
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

    # Tools discovered from an upstream server: already built, so they are added
    # rather than derived.
    for proxied in getattr(app, "_mcp_prebuilt_tools", ()):
        registry.add(proxied)

    # Sub-apps mounted with `expose_mcp=True` contribute their tools under the
    # mount's namespace. Building a sub-app's registry merges its own mounts
    # first, so nesting composes.
    for namespace, sub_app in mcp_mounts(app):
        for tool in build_registry(sub_app).tools.values():
            registry.add(renamed(tool, namespace))

    return registry
