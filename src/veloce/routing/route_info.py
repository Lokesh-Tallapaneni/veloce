"""A route's record, and what a match hands back.

The record layer of `routing/`: what a registration is remembered as, the MCP
options hanging off it, and the tuple a lookup returns. `router.py` re-imports
these, so `veloce.routing.router.RouteInfo` still resolves.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, get_origin

from veloce._constants import MSG_SUCCESSFUL_RESPONSE
from veloce._model_backend import ModelBackend, backend_of
from veloce.status import HTTP_200_OK

RouteHandler = Callable[..., Coroutine[Any, Any, Any]]


@dataclass(frozen=True, slots=True)
class MCPRouteOptions:
    """Everything a route declares for `contrib.mcp`, in one record.

    Twelve fields in one record rather than twelve slots on `RouteInfo`, which
    the router reads none of. `RouteInfo.mcp` is `None` for a route declaring no
    MCP exposure - almost every route - so an app that never mounts MCP carries
    one slot rather than twelve, and the names stay readable as properties.
    """

    expose_as_mcp_tool: bool = False
    mcp_description: str | None = None
    expose_as_mcp_resource: bool = False
    mcp_resource_uri: str | None = None
    mcp_resource_mime_type: str | None = None
    mcp_meta: dict[str, Any] | None = None
    mcp_resource_size: int | None = None
    mcp_resource_annotations: dict[str, Any] | None = None
    mcp_scopes: frozenset[str] | None = None
    mcp_icons: tuple[Any, ...] | None = None
    mcp_task_support: bool = False
    mcp_annotations: dict[str, Any] | None = None

    @classmethod
    def build(cls, **options: Any) -> MCPRouteOptions | None:
        """Return the options, or `None` when the route declares no MCP exposure.

        `mcp_scopes` and `mcp_icons` are normalised to a `frozenset` and a tuple,
        so the record is immutable all the way down.
        """
        # Every value is falsy exactly when it is the default, so one `any()`
        # settles it without the twelve field comparisons a record would cost.
        if not any(options.values()):
            return None
        scopes = options["mcp_scopes"]
        icons = options["mcp_icons"]
        options["mcp_scopes"] = frozenset(scopes) if scopes else None
        options["mcp_icons"] = tuple(icons) if icons else None
        return cls(**options)


# Distinguishes "not supplied" - derive the model from the handler's return
# annotation - from an explicit `response_model=None`, which declares none.
_INFER_RESPONSE_MODEL: Any = object()


@dataclass(slots=True)
class RouteOptions:
    """Every per-route option `add_route` and `route` accept, as one record.

    The public signatures stay spelled out so a route's options are documented
    and completable at the call site; internally they are collected here once
    and passed as a single object, rather than being re-listed at each hand-off
    between `route`, `add_route`, `RouteInfo` and the blueprint and router
    merge paths. A field added here is forwarded by all of them.

    Not frozen: a frozen dataclass sets each of its 39 fields through
    `object.__setattr__`, which measured as most of the cost this record adds to
    registering a route. Nothing mutates it after construction.
    """

    dependencies: list[Any] | None = None
    response_model: Any = _INFER_RESPONSE_MODEL
    tags: list[str] | None = None
    summary: str | None = None
    name: str | None = None
    description: str | None = None
    deprecated: bool = False
    response_description: str = MSG_SUCCESSFUL_RESPONSE
    status_code: int = HTTP_200_OK
    response_class: Any = None
    response_model_include: set[str] | None = None
    response_model_exclude: set[str] | None = None
    response_model_exclude_unset: bool = False
    response_model_exclude_defaults: bool = False
    response_model_by_alias: bool = False
    response_model_exclude_none: bool = False
    include_in_schema: bool = True
    responses: dict[int, dict[str, Any]] | None = None
    operation_id: str | None = None
    openapi_extra: dict[str, Any] | None = None
    defaults: dict[str, Any] | None = None
    callbacks: dict[str, Any] | None = None
    strict_slashes: bool | None = None
    subdomain: str | None = None
    host: str | None = None
    expose_as_mcp_tool: bool = False
    mcp_description: str | None = None
    expose_as_mcp_resource: bool = False
    mcp_resource_uri: str | None = None
    mcp_resource_mime_type: str | None = None
    mcp_meta: dict[str, Any] | None = None
    mcp_resource_size: int | None = None
    mcp_resource_annotations: dict[str, Any] | None = None
    mcp_scopes: Sequence[str] | None = None
    mcp_icons: Sequence[Any] | None = None
    mcp_task_support: bool = False
    mcp_annotations: dict[str, Any] | None = None
    exclude_middleware: Sequence[str | type[Any]] | None = None
    stream: bool = False

    @classmethod
    def from_info(cls, info: RouteInfo, *, name: str | None = None) -> RouteOptions:
        """Read the options a registered route was built from back off `info`.

        Args:
            info: The registered route to read from.
            name: Endpoint name to record instead of `info.name`.

        Returns:
            The options, ready to register the same route again.

        """
        excluded = info.excluded_middleware
        return cls(
            dependencies=info.dependencies,
            response_model=info.response_model,
            tags=info.tags,
            summary=info.summary,
            description=info.description,
            deprecated=info.deprecated,
            response_description=info.response_description,
            status_code=info.status_code,
            response_class=info.response_class,
            response_model_include=info.response_model_include,
            response_model_exclude=info.response_model_exclude,
            response_model_exclude_unset=info.response_model_exclude_unset,
            response_model_exclude_defaults=info.response_model_exclude_defaults,
            response_model_by_alias=info.response_model_by_alias,
            response_model_exclude_none=info.response_model_exclude_none,
            include_in_schema=info.include_in_schema,
            responses=info.responses,
            operation_id=info.operation_id,
            openapi_extra=info.openapi_extra,
            defaults=info.defaults,
            callbacks=info.callbacks,
            strict_slashes=info.strict_slashes,
            subdomain=info.subdomain,
            host=info.host,
            expose_as_mcp_tool=info.expose_as_mcp_tool,
            mcp_description=info.mcp_description,
            expose_as_mcp_resource=info.expose_as_mcp_resource,
            mcp_resource_uri=info.mcp_resource_uri,
            mcp_resource_mime_type=info.mcp_resource_mime_type,
            mcp_meta=info.mcp_meta,
            mcp_resource_size=info.mcp_resource_size,
            mcp_resource_annotations=info.mcp_resource_annotations,
            mcp_icons=info.mcp_icons,
            mcp_task_support=info.mcp_task_support,
            mcp_annotations=info.mcp_annotations,
            stream=info.stream,
            name=name if name is not None else info.name,
            mcp_scopes=list(info.mcp_scopes) if info.mcp_scopes else None,
            exclude_middleware=[*excluded[0], *excluded[1]] if excluded else None,
        )


class RouteInfo:
    """Stored route metadata."""

    __slots__ = (
        "handler",
        "param_names",
        "dependencies",
        "response_model",
        "tags",
        "summary",
        "name",
        "path_template",
        "description",
        "deprecated",
        "response_description",
        "status_code",
        "response_class",
        "response_model_include",
        "response_model_exclude",
        "response_model_exclude_unset",
        "response_model_exclude_defaults",
        "response_model_by_alias",
        "response_model_exclude_none",
        "response_dump_kwargs",
        "response_model_origin",
        "response_model_backend",
        "include_in_schema",
        "responses",
        "operation_id",
        "openapi_extra",
        "defaults",
        "callbacks",
        "handler_plan",
        "route_dep_plans",
        "is_trivial_plan",
        "is_request_only_plan",
        "request_param_name",
        "is_fast_eligible",
        "subdomain",
        "host",
        "mcp",
        "excluded_middleware",
        "stream",
        "ws_messages",
        "strict_slashes",
        "_mw_chain_cache",
    )

    def __init__(
        self,
        handler: RouteHandler,
        param_names: list[str],
        path_template: str,
        options: RouteOptions,
        excluded_middleware: tuple[frozenset[str], tuple[type[Any], ...]] | None = None,
        *,
        dependencies: list[Any] | None,
        response_model: Any,
        tags: list[str] | None,
        name: str | None,
        response_class: Any,
        responses: dict[int, dict[str, Any]] | None,
    ) -> None:
        # The six keyword-only values are the ones registration derives from the
        # router it lands on (its dependencies, tags, responses and default
        # response class, the inferred model, the settled name). They are passed
        # in rather than copied into a second `RouteOptions`: building and
        # `dataclasses.replace`-ing a 39-field record per route added about 30%
        # to registration. The rest is read from `options` once, so the body
        # below reads as it always has; none of these are touched afterwards.
        summary = options.summary
        description = options.description
        deprecated = options.deprecated
        response_description = options.response_description
        status_code = options.status_code
        response_model_include = options.response_model_include
        response_model_exclude = options.response_model_exclude
        response_model_exclude_unset = options.response_model_exclude_unset
        response_model_exclude_defaults = options.response_model_exclude_defaults
        response_model_by_alias = options.response_model_by_alias
        response_model_exclude_none = options.response_model_exclude_none
        include_in_schema = options.include_in_schema
        operation_id = options.operation_id
        openapi_extra = options.openapi_extra
        defaults = options.defaults
        callbacks = options.callbacks
        subdomain = options.subdomain
        host = options.host
        self.handler = handler
        self.param_names = param_names
        self.dependencies = dependencies or []
        self.response_model = response_model
        self.tags = tags or []
        self.summary = summary or ""
        self.name = name or (handler.__name__ if handler is not None else "")
        self.path_template = path_template
        self.description = description or (handler.__doc__ or "")
        self.deprecated = deprecated
        self.response_description = response_description
        self.status_code = status_code
        self.response_class = response_class
        self.response_model_include = (
            set(response_model_include) if response_model_include else None
        )
        self.response_model_exclude = (
            set(response_model_exclude) if response_model_exclude else None
        )
        self.response_model_exclude_unset = response_model_exclude_unset
        self.response_model_exclude_defaults = response_model_exclude_defaults
        self.response_model_by_alias = response_model_by_alias
        self.response_model_exclude_none = response_model_exclude_none
        # Fixed here rather than rebuilt per response; a falsy option is omitted.
        dump_kwargs: dict[str, Any] = {}
        if response_model_exclude_unset:
            dump_kwargs["exclude_unset"] = True
        if response_model_exclude_defaults:
            dump_kwargs["exclude_defaults"] = True
        if response_model_by_alias:
            dump_kwargs["by_alias"] = True
        if response_model_exclude_none:
            dump_kwargs["exclude_none"] = True
        if self.response_model_include:
            dump_kwargs["include"] = self.response_model_include
        if self.response_model_exclude:
            dump_kwargs["exclude"] = self.response_model_exclude
        self.response_dump_kwargs = dump_kwargs
        # Classified once: `get_origin` and the backend probe measured 457 ns
        # and 624 ns, and both are pure functions of `response_model`.
        self.response_model_origin = get_origin(response_model) if response_model else None
        self.response_model_backend = (
            backend_of(response_model)
            if response_model is not None and self.response_model_origin is None
            else ModelBackend.NONE
        )
        self.include_in_schema = include_in_schema
        self.responses = responses or {}
        # Falls back to the route name during schema emission.
        self.operation_id = operation_id
        # Deep-merged into this route's OpenAPI operation object.
        self.openapi_extra = openapi_extra
        # Merged into `path_params` at dispatch, never overriding a URL match.
        self.defaults = defaults or {}
        # Emitted verbatim into the operation's `callbacks` (OpenAPI 3.1 Sec. 4.8.8).
        self.callbacks = callbacks
        # Matched against the request's host at dispatch. `None` is any host;
        # `"*"` is any subdomain of SERVER_NAME but not the apex.
        self.subdomain = subdomain
        # Lower-cased here so dispatch compares without re-lowering per request.
        self.host = host.lower() if host is not None else None
        # `None` leaves the resolver on its build-plan-on-demand path.
        self.handler_plan: Any = None
        self.route_dep_plans: list[Any] = []
        # No injected parameters and no dependencies: dispatch skips the resolver.
        self.is_trivial_plan = False
        self.is_request_only_plan = False
        #: So dispatch binds the injected `Request` without walking the plan.
        self.request_param_name = "request"
        # An async trivial/request-only plan with no other per-route shaping.
        self.is_fast_eligible = False
        # Opt-in request-body streaming: dispatch does not eagerly buffer, so
        # the synchronous body accessors are unavailable until it is drained.
        self.stream = options.stream
        # The message contract of a typed `websocket_listener`, else `None`.
        self.ws_messages: Any = None
        # Carried here so `_readd_route` can rebuild the route without losing it.
        self.strict_slashes: bool | None = options.strict_slashes
        # Every MCP option in one record; `None` when the route declares none.
        self.mcp = MCPRouteOptions.build(
            expose_as_mcp_tool=options.expose_as_mcp_tool,
            mcp_description=options.mcp_description,
            expose_as_mcp_resource=options.expose_as_mcp_resource,
            mcp_resource_uri=options.mcp_resource_uri,
            mcp_resource_mime_type=options.mcp_resource_mime_type,
            mcp_meta=options.mcp_meta,
            mcp_resource_size=options.mcp_resource_size,
            mcp_resource_annotations=options.mcp_resource_annotations,
            mcp_scopes=options.mcp_scopes,
            mcp_icons=options.mcp_icons,
            mcp_task_support=options.mcp_task_support,
            mcp_annotations=options.mcp_annotations,
        )
        # `None` lets the hot path iterate the app's list directly.
        #: A name matches exactly; a type matches by `isinstance`.
        self.excluded_middleware: tuple[frozenset[str], tuple[type[Any], ...]] | None = (
            excluded_middleware
        )
        self._mw_chain_cache: tuple[int, list[Any], list[Any]] | None = None

    # ── MCP options, read through the one record that holds them ──
    # Each accessor repeats its field's declared type rather than widening to
    # `Any`: `RouteInfo` is subpackage-public and `contrib.mcp` reads all of it.

    @property
    def expose_as_mcp_tool(self) -> bool:
        """True when the route is exposed as an MCP tool."""
        return False if self.mcp is None else self.mcp.expose_as_mcp_tool

    @property
    def mcp_description(self) -> str | None:
        """The LLM-facing description of the route's MCP tool."""
        return None if self.mcp is None else self.mcp.mcp_description

    @property
    def expose_as_mcp_resource(self) -> bool:
        """True when the route is exposed as an MCP resource."""
        return False if self.mcp is None else self.mcp.expose_as_mcp_resource

    @property
    def mcp_resource_uri(self) -> str | None:
        """The URI the route's MCP resource is served under."""
        return None if self.mcp is None else self.mcp.mcp_resource_uri

    @property
    def mcp_resource_mime_type(self) -> str | None:
        """The MIME type declared for the route's MCP resource."""
        return None if self.mcp is None else self.mcp.mcp_resource_mime_type

    @property
    def mcp_meta(self) -> dict[str, Any] | None:
        """The extension metadata attached to the route's MCP entry."""
        return None if self.mcp is None else self.mcp.mcp_meta

    @property
    def mcp_resource_size(self) -> int | None:
        """The declared byte size of the route's MCP resource."""
        return None if self.mcp is None else self.mcp.mcp_resource_size

    @property
    def mcp_resource_annotations(self) -> dict[str, Any] | None:
        """The annotations attached to the route's MCP resource."""
        return None if self.mcp is None else self.mcp.mcp_resource_annotations

    @property
    def mcp_scopes(self) -> frozenset[str] | None:
        """The scopes a caller needs to see or call the route over MCP."""
        return None if self.mcp is None else self.mcp.mcp_scopes

    @property
    def mcp_icons(self) -> tuple[Any, ...] | None:
        """The icons a client may render next to the route's MCP entry."""
        return None if self.mcp is None else self.mcp.mcp_icons

    @property
    def mcp_task_support(self) -> bool:
        """True when the route's MCP tool may run as a task."""
        return False if self.mcp is None else self.mcp.mcp_task_support

    @property
    def mcp_annotations(self) -> dict[str, Any] | None:
        """The annotations attached to the route's MCP tool."""
        return None if self.mcp is None else self.mcp.mcp_annotations


class RouteMatch:
    """Result of matching a path against the tree."""

    __slots__ = ("route_info", "path_params")

    def __init__(self, route_info: RouteInfo, path_params: dict[str, Any]) -> None:
        self.route_info = route_info
        self.path_params = path_params
