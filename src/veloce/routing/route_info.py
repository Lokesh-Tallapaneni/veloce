"""A route's record, and what a match hands back.

The record layer of `routing/`: what a registration is remembered as, the MCP
options hanging off it, and the tuple a lookup returns. Separated from
`router.py` because a reader looking for the match algorithm was scrolling past
close to 350 lines of response-model and MCP metadata to reach it.

`router.py` re-imports these, so `veloce.routing.router.RouteInfo` still
resolves.
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

    Eleven fields in one record rather than eleven slots on `RouteInfo`. The
    router reads none of them - they exist for an optional integration - and
    carried flat each has to be enumerated in five places: `RouteInfo.__slots__`,
    its `__init__` signature, its assignment block, the copy-construction in
    `_merge_node`, and `_ROUTE_IDENTITY_SLOTS`. A twelfth would mean editing all
    five, and missing one is silent.

    `RouteInfo.mcp` is `None` for a route that declares no MCP exposure, which
    is almost every route, so an app that never mounts MCP carries one slot
    rather than eleven. The individual names stay readable as properties on
    `RouteInfo`, so nothing reading `info.mcp_scopes` had to change.
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

    @classmethod
    def build(cls, **options: Any) -> MCPRouteOptions | None:
        """Return the options, or `None` when the route declares no MCP exposure.

        `mcp_scopes` and `mcp_icons` are normalised here - to a `frozenset` and a
        tuple - so the record is immutable all the way down and the MCP registry
        reads them directly.
        """
        # Almost every route declares nothing, so that case allocates nothing
        # and compares nothing: every value is falsy exactly when it is the
        # default (two `False` flags, nine `None`s), so one `any()` over the
        # values settles it. Building the record and comparing it against an
        # all-defaults instance costs eleven field comparisons per route, which
        # is measurable at registration on an app with many routes.
        if not any(options.values()):
            return None
        scopes = options["mcp_scopes"]
        icons = options["mcp_icons"]
        options["mcp_scopes"] = frozenset(scopes) if scopes else None
        options["mcp_icons"] = tuple(icons) if icons else None
        return cls(**options)


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
        dependencies: list[Any] | None = None,
        response_model: Any = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        name: str | None = None,
        path_template: str = "",
        description: str | None = None,
        deprecated: bool = False,
        response_description: str = MSG_SUCCESSFUL_RESPONSE,
        status_code: int = HTTP_200_OK,
        response_class: Any = None,
        response_model_include: set[str] | None = None,
        response_model_exclude: set[str] | None = None,
        response_model_exclude_unset: bool = False,
        response_model_exclude_defaults: bool = False,
        response_model_by_alias: bool = False,
        response_model_exclude_none: bool = False,
        include_in_schema: bool = True,
        responses: dict[int, dict[str, Any]] | None = None,
        operation_id: str | None = None,
        openapi_extra: dict[str, Any] | None = None,
        defaults: dict[str, Any] | None = None,
        callbacks: dict[str, Any] | None = None,
        subdomain: str | None = None,
        host: str | None = None,
        expose_as_mcp_tool: bool = False,
        mcp_description: str | None = None,
        expose_as_mcp_resource: bool = False,
        mcp_resource_uri: str | None = None,
        mcp_resource_mime_type: str | None = None,
        mcp_meta: dict[str, Any] | None = None,
        mcp_resource_size: int | None = None,
        mcp_resource_annotations: dict[str, Any] | None = None,
        mcp_scopes: Sequence[str] | None = None,
        mcp_icons: Sequence[Any] | None = None,
        mcp_task_support: bool = False,
        excluded_middleware: tuple[frozenset[str], tuple[type, ...]] | None = None,
    ) -> None:
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
        # The `model_dump` options this route will pass on every response. They
        # are constructor arguments and nothing assigns them afterwards, so the
        # mapping is fixed here rather than rebuilt per response - which the
        # dispatcher did on every request, on most routes, since a return
        # annotation supplies a response model without one being declared.
        # A falsy option is omitted rather than passed as False, preserving the
        # exact call the per-request build produced.
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
        # How to shape this route's responses, classified once. `get_origin` and
        # the backend probe are pure functions of `response_model`, and they
        # measured 457 ns and 624 ns - paid on every response before this.
        self.response_model_origin = get_origin(response_model) if response_model else None
        self.response_model_backend = (
            backend_of(response_model)
            if response_model is not None and self.response_model_origin is None
            else ModelBackend.NONE
        )
        self.include_in_schema = include_in_schema
        self.responses = responses or {}
        # Explicit OpenAPI `operationId` override; falls back to the route
        # name during schema emission.
        self.operation_id = operation_id
        # `openapi_extra` - an arbitrary dict deep-merged into
        # this route's OpenAPI operation object (lets users inject
        # vendor extensions, custom requestBody examples, etc.).
        self.openapi_extra = openapi_extra
        # The routing-rule `defaults`: fixed values merged into
        # `path_params` at dispatch (without overriding URL-matched
        # params), so two rules can share one handler with one rule
        # supplying a default for a segment the other carries in the URL.
        self.defaults = defaults or {}
        # OpenAPI `callbacks` - a dict of named Callback objects emitted
        # verbatim into the operation's `callbacks` field (OpenAPI 3.1
        # Sec. 4.8.8 - out-of-band requests the API issues back to a caller).
        self.callbacks = callbacks
        # Subdomain constraint - matched against the request's host at
        # dispatch time. `None` means "any host"; `"*"` means "any
        # subdomain of SERVER_NAME but not the apex".
        self.subdomain = subdomain
        # Host constraint - the full `Host` header must equal this value
        # exactly (case-insensitive). `None` means "any host". Broader
        # than `subdomain`, which only constrains the leftmost label.
        # Normalised to lower case here so dispatch can compare without
        # re-lowering on every request.
        self.host = host.lower() if host is not None else None
        # Pre-computed reflection plan - filled in by Router.add_route once
        # this RouteInfo has been constructed. Tests that build RouteInfo
        # directly will see `None` here and the resolver will fall back to
        # the build-plan-on-demand path.
        self.handler_plan: Any = None
        self.route_dep_plans: list[Any] = []
        # True when the handler takes no injected parameters and the route
        # carries no dependencies - the dispatcher then skips the
        # dependency resolver entirely (the "trivial-route" fast path).
        # Set by `_finalize_plans`, which `add_route` calls once the plans
        # are built.
        self.is_trivial_plan = False
        self.is_request_only_plan = False
        #: The handler's parameter name for the injected `Request` on a
        #: request-only plan, so dispatch binds it without walking
        #: `handler_plan.slots[0].name` per request. Fixed at registration.
        self.request_param_name = "request"
        # True when this route can take the straight-line dispatch fast path:
        # an async trivial/request-only plan with no response_model, custom
        # response_class, non-default status, subdomain/host constraint,
        # defaults, or middleware exclusion. Set by `_finalize_plans`; left
        # False for synthetic routes that bypass it.
        self.is_fast_eligible = False
        # Opt-in request-body streaming (ASGI path). When True, the dispatch
        # layer does NOT eagerly buffer the body before the handler, so the
        # handler can consume `request.stream()` incrementally; the synchronous
        # body accessors (`.get_json()`/`.form`/`.data`) are unavailable on such
        # a route until the body is drained. Set by `add_route`; default False
        # preserves the buffer-before-handler behaviour every other route has.
        self.stream = False
        # What this channel's messages are, for a typed `websocket_listener`.
        # `None` on every other route - raw websockets, untyped listeners,
        # `text`/`bytes` listeners, and all HTTP routes - so a route that
        # declares no message contract carries one `None` and nothing else.
        # Set by `_finalize_plans`, read off the handler the listener wrapper
        # built, so every registration path carries it without forwarding it.
        self.ws_messages: Any = None
        # The slash-matching mode this route was declared with. It shapes the
        # radix node and the regex route rather than the request, so it lived
        # only on those - and `_readd_route`, which rebuilds a route from its
        # `RouteInfo`, had nothing to read. A blueprint route declared
        # `strict_slashes=False` therefore lost it on registration while the
        # same route reached through `include_router` kept it.
        self.strict_slashes: bool | None = None
        # Every MCP option in one record. See `MCPRouteOptions`: the router
        # reads none of them, and carried flat they had to be enumerated in five
        # places. `None` when the route declares no MCP exposure.
        self.mcp = MCPRouteOptions.build(
            expose_as_mcp_tool=expose_as_mcp_tool,
            mcp_description=mcp_description,
            expose_as_mcp_resource=expose_as_mcp_resource,
            mcp_resource_uri=mcp_resource_uri,
            mcp_resource_mime_type=mcp_resource_mime_type,
            mcp_meta=mcp_meta,
            mcp_resource_size=mcp_resource_size,
            mcp_resource_annotations=mcp_resource_annotations,
            mcp_scopes=mcp_scopes,
            mcp_icons=mcp_icons,
            mcp_task_support=mcp_task_support,
        )
        # Named middleware this route opts out of. `None` (the common case)
        # means "run every registered middleware" - the dispatch hot path
        # then iterates the app's middleware list directly with zero extra
        # work. A non-`None` frozenset triggers the filtered-chain path,
        # whose result is memoised in `_mw_chain_cache` keyed on the app's
        # middleware-list version so the filter runs at most once per
        # (route, middleware-set) generation, not per request.
        #: `(names, types)` for a route that opts out of middleware, else `None`.
        #: A name matches `Middleware.middleware_name` exactly; a type matches by
        #: `isinstance`, so it covers subclasses.
        self.excluded_middleware: tuple[frozenset[str], tuple[type, ...]] | None = (
            excluded_middleware
        )
        self._mw_chain_cache: tuple[int, list[Any], list[Any]] | None = None

    # ── MCP options, read through the one record that holds them ──
    #
    # Each accessor repeats its field's declared type from `MCPRouteOptions`
    # rather than widening to `Any`: `RouteInfo` is subpackage-public, and
    # `contrib.mcp` reads every one of these through the property. The
    # no-record branch needs no widening - `False` and `None` already
    # inhabit the field type each one returns.

    @property
    def expose_as_mcp_tool(self) -> bool:
        return False if self.mcp is None else self.mcp.expose_as_mcp_tool

    @property
    def mcp_description(self) -> str | None:
        return None if self.mcp is None else self.mcp.mcp_description

    @property
    def expose_as_mcp_resource(self) -> bool:
        return False if self.mcp is None else self.mcp.expose_as_mcp_resource

    @property
    def mcp_resource_uri(self) -> str | None:
        return None if self.mcp is None else self.mcp.mcp_resource_uri

    @property
    def mcp_resource_mime_type(self) -> str | None:
        return None if self.mcp is None else self.mcp.mcp_resource_mime_type

    @property
    def mcp_meta(self) -> dict[str, Any] | None:
        return None if self.mcp is None else self.mcp.mcp_meta

    @property
    def mcp_resource_size(self) -> int | None:
        return None if self.mcp is None else self.mcp.mcp_resource_size

    @property
    def mcp_resource_annotations(self) -> dict[str, Any] | None:
        return None if self.mcp is None else self.mcp.mcp_resource_annotations

    @property
    def mcp_scopes(self) -> frozenset[str] | None:
        return None if self.mcp is None else self.mcp.mcp_scopes

    @property
    def mcp_icons(self) -> tuple[Any, ...] | None:
        return None if self.mcp is None else self.mcp.mcp_icons

    @property
    def mcp_task_support(self) -> bool:
        return False if self.mcp is None else self.mcp.mcp_task_support


class RouteMatch:
    """Result of matching a path against the tree."""

    __slots__ = ("route_info", "path_params")

    def __init__(self, route_info: RouteInfo, path_params: dict[str, Any]) -> None:
        self.route_info = route_info
        self.path_params = path_params
