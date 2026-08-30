"""Router — radix-tree routing with path parameters, method dispatch, and route groups."""

from __future__ import annotations

import functools
import keyword
import logging
import re
from collections.abc import Callable, Coroutine, Iterator, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, get_origin
from urllib.parse import urlencode

from typing_extensions import Doc

from veloce._constants import MSG_SUCCESSFUL_RESPONSE
from veloce._handler_plan import K_REQUEST, build_plan, build_route_dep_plans
from veloce._model_backend import ModelBackend, backend_of, resolve_response_contract
from veloce._protocol_constants import (
    HTTP_METHOD_DELETE,
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
    HTTP_METHOD_PATCH,
    HTTP_METHOD_POST,
    HTTP_METHOD_PUT,
    HTTP_METHOD_QUERY,
    HTTP_METHOD_TRACE,
    ROUTE_METHOD_WEBSOCKET,
    URL_SCHEME_HTTP,
)
from veloce._ws_listener import build_listener_handler
from veloce.exceptions import DuplicateRouteError
from veloce.middleware.base import Middleware
from veloce.routing.converters import (
    Converter,
    StringConverter,
    _is_parametrized_spec,
    _iter_placeholders,
    _looks_like_regex,
    build_route_regex,
    extract_regex_converters,
    is_regex_path,
    parse_converter,
)
from veloce.status import HTTP_200_OK

RouteHandler = Callable[..., Coroutine[Any, Any, Any]]

_logger = logging.getLogger(__name__)

# Default for `response_model=`, distinguishing "not supplied" (derive the model
# from the handler's return annotation) from an explicit `response_model=None`
# (declare no response contract, even when the handler is annotated). Without the
# sentinel a route could not keep a model return annotation for its type checker
# while opting out of filtering and the OpenAPI response schema.
_INFER_RESPONSE_MODEL: Any = object()

# ── Module constants and helpers ───────────────────────────


# Valid `on_duplicate` policies for a router. `"error"` raises, `"warn"` logs
# and replaces, `"override"` replaces silently.
_DUPLICATE_POLICIES = frozenset({"error", "warn", "override"})

# Parameter documentation `add_route` and `route` both publish. These reach a
# user through IDE tooltips and the generated reference, so whichever entry
# point their editor resolves decides what they read - and four of these had
# already drifted, losing a caveat on one side only. Shared objects instead of
# parallel copies, so a change reaches both signatures at once.
_DOC_MCP_RESOURCE_URI = Doc(
    "Resource URI for the route's MCP resource: a static URI, or a URI "
    "template (`users://{user_id}`) binding its path parameters."
)
_DOC_MCP_RESOURCE_MIME_TYPE = Doc(
    "Media type advertised for the route's MCP resource. Declared rather "
    "than inferred, so the listing never disagrees with what a read returns."
)
_DOC_MCP_META = Doc(
    "`_meta` published on this route's MCP tool or resource, for metadata "
    "an extension defines rather than the protocol itself."
)
_DOC_MCP_TASK_SUPPORT = Doc(
    "Allow this route's MCP tool to run as a background task "
    "(task-augmented `tools/call`, polled via `tasks/get` / `tasks/result`)."
)
_DOC_RESPONSE_MODEL = Doc(
    "Type used to filter and serialize the handler's return value and the OpenAPI "
    "response schema. Defaults to the handler's return annotation when it names a "
    "model; pass `None` to declare no response contract."
)
_DOC_STREAM = Doc(
    "Opt into request-body streaming: the body is not buffered before the "
    "handler, so the handler may consume `request.stream()` incrementally. "
    "The synchronous body accessors are unavailable on a streaming route "
    "until the body is drained."
)
_DOC_EXCLUDE_MIDDLEWARE = Doc(
    "Middleware this route opts out of, as classes or as resolved names. A "
    "class matches by type, so it covers subclasses and cannot be misspelled; "
    "a string matches the middleware's resolved `name` exactly, which is how "
    "two instances of one class are told apart."
)


def _split_exclusions(
    entries: Sequence[str | type] | None,
) -> tuple[frozenset[str], tuple[type, ...]] | None:
    """Split `exclude_middleware` into its name set and its type tuple.

    `None` when the route excludes nothing, which the dispatch path tests for
    before doing any filtering. An entry that is neither a name nor a
    `Middleware` subclass is a `TypeError` here rather than an exclusion that
    silently matches nothing - which is the failure this shape exists to end.
    """
    if not entries:
        return None
    names: set[str] = set()
    types: list[type] = []
    for entry in entries:
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, type) and issubclass(entry, Middleware):
            types.append(entry)
        else:
            raise TypeError(
                "exclude_middleware entries must be a middleware class or a "
                f"middleware name, got {entry!r}"
            )
    return frozenset(names), tuple(types)


# Normalize an OpenAPI-style path to its parameter-name-agnostic shape:
# `/items/{slug}` and `/items/{id}` both become `/items/{}`. Used to detect
# when a tree route and a regex fallback route map to the same effective path.
_PARAM_SHAPE_RE = re.compile(r"\{[^{}]*\}")


def _path_shape(path: str) -> str:
    """Return `path` with every `{param}` collapsed to `{}` for shape compare."""
    return _PARAM_SHAPE_RE.sub("{}", path)


@functools.lru_cache(maxsize=512)
def _cached_split_path(path: str) -> tuple[str, ...]:
    return tuple(s for s in path.split("/") if s)


def _reverse_converters_for(template: str) -> dict[str, Converter]:
    """Map each typed placeholder in `template` to its converter for url_for.

    A bare `{name}` (no spec) and a raw-regex placeholder (`{id:[0-9]+}`) have
    no single coercing converter, so they are omitted - those params accept any
    stringifiable value during reverse. Built-in, custom, and `any(...)` specs
    map to the same converter the radix matcher applies, so url_for can reject a
    value the matcher would never accept. A bare or raw-regex placeholder is
    therefore *not* validated - `url_for` guarantees resolvability only for the
    placeholders this returns, which is why the omission is stated here rather
    than promised away. `any(...)` is whitelisted explicitly
    because it carries parentheses that the bare-identifier test reads as regex.
    """
    converters: dict[str, Converter] = {}
    for ph in _iter_placeholders(template):
        spec = ph.spec
        if not spec:
            continue
        is_any = spec.startswith("any(") and spec.endswith(")")
        # A parametrized built-in (`int(min=1)`, `str(length=2)`) carries
        # parens that read as regex, but it maps to a real coercing converter,
        # so reverse it too and let url_for reject an out-of-bounds value.
        if not is_any and not _is_parametrized_spec(spec) and _looks_like_regex(spec):
            continue
        converters[ph.name] = parse_converter(spec)
    return converters


def _check_duplicate_params(full_path: str) -> None:
    """Reject a path whose parameter names are illegal or bound twice.

    Path parameters are passed to the handler as keyword arguments, so a name
    that is not a legal Python identifier (or is a reserved keyword) can never
    bind and would otherwise fail opaquely deep in the handler-plan binder at
    request time. A duplicate is likewise always a bug: on the radix path the
    second capture silently clobbers the first at match time; on the regex path
    `re.compile` raises an opaque "redefinition of group name" error. Catch all
    three at registration with one clear, path-scoped error, using the same
    placeholder scanner both branches consume so the names checked are exactly
    the names bound.
    """
    seen: set[str] = set()
    for ph in _iter_placeholders(full_path):
        if not ph.name.isidentifier() or keyword.iskeyword(ph.name):
            raise ValueError(
                f"Route {full_path!r}: invalid path parameter name {ph.name!r}; "
                "a parameter name must be a valid Python identifier and not a keyword"
            )
        if ph.name in seen:
            raise ValueError(f"Route {full_path!r}: duplicate path parameter {ph.name!r}")
        seen.add(ph.name)


# Static and wildcard nodes carry no parameter, so their converter is never
# consulted. A shared instance keeps the slot non-optional - the same choice
# `param_name` makes with `""` - because a `| None` here reaches the match loop,
# where narrowing it would cost a check per param child per request.
_NO_CONVERTER = StringConverter()


def _converter_sort_key(node: RadixNode) -> int:
    """Order competing parameter segments, most restrictive first.

    The value comes from the converter's own `specificity`, not from a table
    here; `Converter.specificity` documents why it is declared there.

    Runs once per `add_route` at startup, never on the per-request match path.
    """
    return node.converter.specificity


# ── Radix tree structures ──────────────────────────────────


class RadixNode:
    """A node in the radix tree."""

    __slots__ = (
        "segment",
        "static_children",
        "param_children",
        "_param_index",
        "wildcard_child",
        "handlers",
        "param_name",
        "is_param",
        "is_wildcard",
        "trailing_slash",
        "unslashed_variant",
        "tolerant_slash",
        "converter",
    )

    def __init__(self, segment: str = "") -> None:
        self.segment = segment
        # Static children are indexed by exact segment for O(1) match-time
        # lookup. Param and wildcard children are few; they stay in their
        # own small containers and are scanned only after a static miss.
        self.static_children: dict[str, RadixNode] = {}
        self.param_children: list[RadixNode] = []
        # O(1) registration-time lookup keyed by (param_name, converter_type,
        # constraint), where `constraint` is the parametrized spec text (e.g.
        # `int(min=1)`) or None for an unparametrized converter. The ordered
        # list above is still the source of truth at match time.
        self._param_index: dict[tuple[str, type, str | None], RadixNode] = {}
        self.wildcard_child: RadixNode | None = None
        # Method name (uppercase) -> RouteInfo.
        self.handlers: dict[str, RouteInfo] = {}
        # `""` rather than `None` for a static node, which has no name. The
        # union would otherwise reach the match loop, where the three
        # `params[pname]` sites would each need a suppression or a runtime
        # narrowing check per param child per request.
        self.param_name: str = ""
        self.is_param = False
        self.is_wildcard = False
        self.trailing_slash = False
        # `/foo` and `/foo/` collapse to the same node, so the two strictness
        # flags are tracked independently: `trailing_slash` records that the
        # slashed form was registered, `unslashed_variant` the unslashed form.
        # When both are set the node serves both shapes and neither strictness
        # gate fires - registering one form must not flip the other to a 404.
        self.unslashed_variant = False
        # When True, the slashed and unslashed forms both match without
        # redirect - set by `strict_slashes=False` on `add_route`.
        self.tolerant_slash = False
        # Converter applied at match time. `add_route` sets it on every param
        # node; static and wildcard nodes keep the placeholder and never read it.
        self.converter: Converter = _NO_CONVERTER


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


# ── Route metadata ─────────────────────────────────────────


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


# ── Regex fallback routes ──────────────────────────────────


def _openapi_path_from_template(template: str) -> str:
    """Reduce a brace template to its OpenAPI path form (`{name}` per param).

    Strips the `:converter` (or raw `:regex`) portion of every placeholder so
    `/users/{id:[0-9]+}` becomes `/users/{id}`. Balance-aware so a spec with
    its own braces (`{id:[0-9]{2}}`) reduces cleanly to `{id}`.
    """
    out: list[str] = []
    pos = 0
    for ph in _iter_placeholders(template):
        out.append(template[pos : ph.start])
        out.append("{" + ph.name + "}")
        pos = ph.end
    out.append(template[pos:])
    return "".join(out)


class RegexRoute:
    """A route the radix tree cannot express, matched by a compiled regex.

    Registered alongside the radix tree but consulted only on a tree miss
    (and only when regex routes exist). The fast path never touches these.
    """

    __slots__ = ("pattern", "template", "param_names", "handlers", "converters", "tolerant_slash")

    def __init__(self, template: str, pattern: re.Pattern[str], param_names: list[str]) -> None:
        # The original brace template (`/users/{id:[0-9]+}`), kept for
        # `url_for` reverse resolution and OpenAPI path emission.
        self.template = template
        self.pattern = pattern
        self.param_names = param_names
        # Method -> RouteInfo, mirroring RadixNode.handlers so the regex
        # path returns the same shape as the tree path.
        self.handlers: dict[str, RouteInfo] = {}
        # Built-in converter per placeholder name, so matched groups are
        # coerced to the same Python types the radix tree produces
        # (`{n:int}` -> int, not "3"). Bare and raw-regex groups are absent.
        self.converters: dict[str, Converter] = extract_regex_converters(template)
        # Mirrors `RadixNode.tolerant_slash` - set by `strict_slashes=False`
        # so a regex route accepts the missing/extra trailing slash too.
        self.tolerant_slash = False

    @property
    def openapi_path(self) -> str:
        """OpenAPI-style path string built from the template (`/users/{id}`)."""
        return _openapi_path_from_template(self.template)


# ── Router ─────────────────────────────────────────────────


def _slash_mismatch(node: Any, request_has_slash: bool) -> bool:
    """Whether trailing-slash strictness rules this node out for this request.

    A route registered with a trailing slash matches only slashed requests, and
    one registered without matches only unslashed ones. `tolerant_slash`
    (per-route `strict_slashes=False`) skips the gate entirely, and a node that
    had *both* forms registered serves both shapes so neither arm fires.

    One predicate because two consumers must agree: `_match_tree` decides
    whether a request routes, and `get_allowed_methods` decides what the `Allow`
    header advertises. Written separately they can disagree, and then a 405
    names a method that would not have matched, or omits one that would.
    """
    if node.tolerant_slash:
        return False
    if node.trailing_slash and not node.unslashed_variant and not request_has_slash:
        return True
    return bool(
        node.unslashed_variant and not node.trailing_slash and request_has_slash and node.handlers
    )


class Router:
    """High-performance radix-tree router with a decorator-based route API.

    Usage::

        from veloce import Router, Veloce

        api = Router(prefix="/api")

        @api.get("/items/{item_id:int}")
        async def get_item(item_id: int):
            return {"item_id": item_id}

        app = Veloce()
        app.include_router(api)
    """

    def __init__(
        self,
        prefix: Annotated[
            str,
            Doc("Path prefix prepended to every route registered on this router."),
        ] = "",
        tags: Annotated[
            list[str] | None,
            Doc("OpenAPI tags applied to every route registered on this router."),
        ] = None,
        default_response_class: Annotated[
            Any,
            Doc("Response class used for routes that do not declare their own `response_class`."),
        ] = None,
        dependencies: Annotated[
            list[Any] | None,
            Doc("Dependencies applied to every route, run before each route's own dependencies."),
        ] = None,
        responses: Annotated[
            dict[int, dict[str, Any]] | None,
            Doc("Additional OpenAPI responses overlaid onto every route on this router."),
        ] = None,
        on_duplicate: Annotated[
            str,
            Doc(
                "Policy for a second handler on the same path and method: `error`, `warn`, or `override`."
            ),
        ] = "error",
    ) -> None:
        self.prefix = prefix.rstrip("/")
        # Policy for a second handler registered on the same path+method:
        # `"error"` (default) raises `DuplicateRouteError`, `"warn"` logs and
        # replaces, `"override"` replaces silently. Catches accidental route
        # shadowing at startup instead of a wrong handler firing in production.
        if on_duplicate not in _DUPLICATE_POLICIES:
            raise ValueError(
                f"on_duplicate must be one of {sorted(_DUPLICATE_POLICIES)}, got {on_duplicate!r}"
            )
        self.on_duplicate = on_duplicate
        # Copied, not aliased: `router.tags` is appended to as routes register,
        # which would otherwise mutate the list the caller still holds.
        self.tags = list(tags or [])
        # The Response subclass used when a registered route does not pick
        # its own `response_class=`. Routes still override per-call. Once set it is the class every return value goes to - a text
        # class given a `dict` raises rather than falling back to JSON, since a
        # route declaring HTML and returning a mapping has stated two things that
        # cannot both hold. Unset, dict/list returns take `JSONResponse`.
        self.default_response_class = default_response_class
        # Router-level dependencies - applied to every route
        # registered on this router. Per-route `dependencies=` is
        # *appended* to (not replaced by) the router-level list, so
        # both fire and the route-specific ones run last.
        self.router_dependencies = list(dependencies or [])
        # Router-level `responses=` dict. Each route's
        # `responses=` overlays on top - per-route status codes win on
        # collision; router-level supplies the rest (typically the
        # 404/403/422 shape every route shares).
        self.router_responses: dict[int, dict[str, Any]] = dict(responses or {})
        self._root = RadixNode()
        # (method, exact-path) -> RouteInfo for literal, strict paths, so a
        # literal path resolves in one hash lookup instead of a radix walk.
        # Built lazily on first match and dropped to None on any registration;
        # every non-literal shape falls through to the tree (see match()).
        self._static_routes: dict[tuple[str, str], RouteInfo] | None = None
        # Route name -> (path_template, param_names), for url_for reverse lookup.
        self._named_routes: dict[str, tuple[str, list[str]]] = {}
        # Route name -> {param_name: converter}, derived from the template on the
        # first url_for call and cached. Lets url_for validate each substituted
        # value through the same converter the matcher applies, so a reversed URL
        # is guaranteed to resolve. A param with no typed converter (bare
        # `{name}` or a raw-regex segment) is omitted and skips validation.
        self._reverse_converters: dict[str, dict[str, Converter]] = {}
        # Regex fallback routes, in registration order. Empty for the common
        # case; `match()` guards on `if self._regex_routes:` so the radix
        # fast path pays nothing when no regex route is registered.
        self._regex_routes: list[RegexRoute] = []
        # Template -> RegexRoute, so a second method on the same regex path
        # reuses one compiled pattern instead of appending a duplicate.
        self._regex_route_index: dict[str, RegexRoute] = {}

    # ── Route registration ────────────────────────────────

    def _split_path(self, path: str) -> tuple[str, ...]:
        """Split path into segments (cached)."""
        return _cached_split_path(path)

    def _insert_path_into_tree(
        self,
        node: RadixNode,
        segments: tuple[str, ...],
        path: str,
    ) -> tuple[RadixNode, list[str]]:
        """Walk `segments` from `node`, creating or reusing radix children.

        Returns the leaf node (where route metadata attaches) and the
        ordered list of path-parameter names encountered along the way.
        Shared by `add_route` and `_merge_node`; both code paths must
        accept or reject the same shapes (notably the greedy `:path`
        converter must be the final segment).
        """
        # Any tree mutation invalidates the literal-path fast map; it rebuilds
        # lazily on the next match() against the final node state.
        self._static_routes = None
        param_names: list[str] = []
        total = len(segments)
        for idx, seg in enumerate(segments):
            if seg.startswith("{") and seg.endswith("}"):
                spec = seg[1:-1]
                if ":" in spec:
                    param_name, _, conv_spec = spec.partition(":")
                else:
                    param_name, conv_spec = spec, ""
                converter = parse_converter(conv_spec) if conv_spec else StringConverter()
                param_names.append(param_name)

                # Reuse an existing param child with the same name AND matching
                # converter type; otherwise add a new one. Different converters
                # for the same name on the same slot would be ambiguous. A
                # parametrized converter (`int(min=1)` vs `int(min=5)`) keeps
                # its constraint text in the key so distinct bounds get their
                # own node instead of silently reusing the first registered one.
                constraint = (
                    conv_spec if "(" in conv_spec and not conv_spec.startswith("any(") else None
                )
                key = (param_name, type(converter), constraint)
                child = node._param_index.get(key)
                if child is None:
                    child = RadixNode(seg)
                    child.is_param = True
                    child.param_name = param_name
                    child.converter = converter
                    node.param_children.append(child)
                    node.param_children.sort(key=_converter_sort_key)
                    node._param_index[key] = child
                node = child
                if converter.greedy:
                    remaining = total - idx - 1
                    if remaining:
                        trailing = segments[idx + 1 :]
                        raise ValueError(
                            f"Route {path!r}: greedy converter {{...:path}} must be the "
                            f"final segment; got {remaining} segment(s) after it: {trailing!r}"
                        )
                    break
            elif seg == "*":
                # Wildcard (legacy `*` syntax). Reuse the slot so two routes
                # registering `*` at the same node share one node.
                child = node.wildcard_child
                if child is None:
                    child = RadixNode(seg)
                    child.is_wildcard = True
                    node.wildcard_child = child
                node = child
                break
            else:
                child = node.static_children.get(seg)
                if child is None:
                    child = RadixNode(seg)
                    node.static_children[seg] = child
                node = child
        return node, param_names

    @staticmethod
    def _handler_qualname(info: RouteInfo) -> str:
        """Best-effort qualified name of a route's handler for error messages."""
        handler = info.handler
        module = getattr(handler, "__module__", None)
        qualname = getattr(handler, "__qualname__", None) or getattr(handler, "__name__", None)
        if qualname is None:
            return repr(handler)
        return f"{module}.{qualname}" if module else qualname

    # Every RouteInfo slot that defines routing/dispatch behavior or shapes the
    # generated OpenAPI document. The idempotent-remount exemption requires ALL
    # of these to match: any difference is a genuine second registration that
    # must obey the `on_duplicate` policy. Listed explicitly (rather than diffed
    # against a hand-picked subset) so adding a route-defining slot to RouteInfo
    # forces a conscious decision here. Excluded are only purely-derived/cached
    # slots - `handler_plan`, `route_dep_plans`, `is_trivial_plan`,
    # `is_request_only_plan`, `_mw_chain_cache` - which `add_route` rebuilds
    # deterministically from the compared fields.
    _ROUTE_IDENTITY_SLOTS: tuple[str, ...] = (
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
        "include_in_schema",
        "responses",
        "operation_id",
        "openapi_extra",
        "defaults",
        "callbacks",
        "subdomain",
        "host",
        "expose_as_mcp_tool",
        "mcp_description",
        "expose_as_mcp_resource",
        "mcp_resource_uri",
        "mcp_resource_mime_type",
        "mcp_meta",
        "mcp_resource_size",
        "mcp_resource_annotations",
        "mcp_scopes",
        "mcp_icons",
        "mcp_task_support",
        "excluded_middleware",
    )

    @classmethod
    def _allow_duplicate(cls, existing: RouteInfo, incoming: RouteInfo) -> bool:
        """Return True when re-registering `incoming` over `existing` is benign.

        An idempotent re-mount - the same handler callable landing on the same
        path+method again with identical route-defining metadata, as happens
        when a router/blueprint is included more than once - is not a conflict
        regardless of policy, so legitimate blueprint merges never
        false-positive.

        The exemption is deliberately narrow: it fires ONLY when the handler is
        the same object AND every route-defining/document-shaping field matches.
        A same-callable registration that carries *different* metadata (name,
        response_model, dependencies, defaults, `exclude_middleware`,
        `response_class`, `openapi_extra`, host/subdomain, ...) is a real second
        registration and must go through the `on_duplicate` policy; comparing a
        subset would silently bypass `on_duplicate='error'` for two distinct
        decorations of one function.
        """
        if existing.handler is not incoming.handler:
            return False
        for slot in cls._ROUTE_IDENTITY_SLOTS:
            if getattr(existing, slot) != getattr(incoming, slot):
                return False
        return True

    def _on_duplicate_route(
        self, path: str, method: str, existing: RouteInfo, incoming: RouteInfo
    ) -> None:
        """Apply the router's `on_duplicate` policy to a route collision."""
        policy = self.on_duplicate
        if policy == "override":
            return
        existing_name = self._handler_qualname(existing)
        incoming_name = self._handler_qualname(incoming)
        if policy == "warn":
            _logger.warning(
                "Duplicate route: %s %s, replacing %s with %s",
                method,
                path,
                existing_name,
                incoming_name,
            )
            return
        raise DuplicateRouteError(path, method, existing_name, incoming_name)

    def _drop_replaced_route_name(
        self,
        replaced: RouteInfo,
        winning_name: str,
        handler_table: dict[str, RouteInfo],
        replaced_method: str,
    ) -> None:
        """Remove the replaced route's reverse entry when a duplicate wins.

        On a `warn`/`override` replace, the incoming route is registered under
        `winning_name`. If the route it displaced carried a *different* `name=`,
        its `_named_routes` entry would otherwise survive and let
        `url_for(old_name)` resolve to a route no longer in the handler table.

        But a multi-method route (e.g. GET+POST under one `RouteInfo` and one
        `name=`) may be replaced for a *single* method only: overriding GET
        leaves the same endpoint live for POST, so `url_for(old_name)` must
        keep working. Drop the reverse entry only when the replaced route is no
        longer the owner of that name anywhere in the table - i.e. no remaining
        live route (this `RouteInfo` under another method, or any other route)
        still carries `old_name`. The slot at `(handler_table, replaced_method)`
        is excluded from the scan because the caller is about to overwrite it
        with the winning route. A same-name replace keeps the entry, since the
        winning registration overwrites it with the correct template anyway.
        """
        old_name = replaced.name
        if not old_name or old_name == winning_name:
            return
        if self._name_still_live(old_name, handler_table, replaced_method):
            return
        self._named_routes.pop(old_name, None)
        self._reverse_converters.pop(old_name, None)

    def _name_still_live(
        self,
        name: str,
        excluded_table: dict[str, RouteInfo],
        excluded_method: str,
    ) -> bool:
        """Whether any live route still carries `name`, ignoring one slot.

        Walks every committed handler table (radix tree + regex routes) and
        reports whether some route's `name` matches. The single slot at
        `(excluded_table, excluded_method)` is skipped because it holds the
        route being displaced and is about to be overwritten by the winner.
        This runs only on a duplicate-route override/warn replace, never on
        the per-request match path.
        """
        for table, method, info in self._iter_live_handlers():
            if table is excluded_table and method == excluded_method:
                continue
            if info.name == name:
                return True
        return False

    def _iter_live_handlers(
        self,
    ) -> Iterator[tuple[dict[str, RouteInfo], str, RouteInfo]]:
        """Yield `(handler_table, method, RouteInfo)` for every live route."""
        stack: list[RadixNode] = [self._root]
        while stack:
            node = stack.pop()
            for method, info in node.handlers.items():
                yield node.handlers, method, info
            stack.extend(node.static_children.values())
            stack.extend(node.param_children)
            if node.wildcard_child is not None:
                stack.append(node.wildcard_child)
        for route in self._regex_routes:
            for method, info in route.handlers.items():
                yield route.handlers, method, info

    def _build_merged_route_info(
        self,
        info: RouteInfo,
        param_names: list[str],
        full_path: str,
    ) -> RouteInfo:
        """Build a `RouteInfo` for a route being merged in from a child router.

        Copies the source route's fields, re-applies this router's
        `router_dependencies`/`tags`/`responses`, and re-binds the merged
        path and param names. Shared by the tree (`_merge_node`) and regex
        (`_merge_regex_routes`) merge paths so they stay in lock-step.
        """
        combined_deps = list(self.router_dependencies)
        if info.dependencies:
            combined_deps.extend(info.dependencies)
        merged = RouteInfo(
            handler=info.handler,
            param_names=param_names,
            dependencies=combined_deps if combined_deps else info.dependencies,
            response_model=info.response_model,
            tags=(info.tags or []) + list(self.tags),
            summary=info.summary,
            name=info.name,
            path_template=full_path,
            description=info.description,
            deprecated=info.deprecated,
            response_description=info.response_description,
            status_code=info.status_code,
            response_class=info.response_class or self.default_response_class,
            response_model_include=info.response_model_include,
            response_model_exclude=info.response_model_exclude,
            response_model_exclude_unset=info.response_model_exclude_unset,
            response_model_exclude_defaults=info.response_model_exclude_defaults,
            response_model_by_alias=info.response_model_by_alias,
            response_model_exclude_none=info.response_model_exclude_none,
            include_in_schema=info.include_in_schema,
            responses=(
                None
                if not self.router_responses and not info.responses
                else {**self.router_responses, **(info.responses or {})}
            ),
            operation_id=info.operation_id,
            # Carry constraints from the source RouteInfo - without these,
            # sub-routers merged via include_router would silently lose their
            # subdomain / host / openapi_extra / defaults / callbacks
            # declarations.
            openapi_extra=info.openapi_extra,
            defaults=info.defaults,
            callbacks=info.callbacks,
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
            mcp_scopes=list(info.mcp_scopes) if info.mcp_scopes else None,
            mcp_icons=info.mcp_icons,
            mcp_task_support=info.mcp_task_support,
            excluded_middleware=info.excluded_middleware,
        )
        # `stream` is a slot assigned after construction rather than an
        # `__init__` argument, so it has to be carried across explicitly; a
        # field added that way is the kind this copy silently drops.
        merged.stream = info.stream
        merged.strict_slashes = info.strict_slashes
        return merged

    def _commit_merged_method(
        self,
        handler_table: dict[str, RouteInfo],
        method: str,
        route_info: RouteInfo,
        source_name: str,
        full_path: str,
        param_names: list[str],
    ) -> None:
        """Apply the duplicate policy and commit one merged method.

        Shared by both merge paths (`_merge_node`, `_merge_regex_routes`):
        runs the collision check, drops the displaced reverse entry when a
        replace wins, writes the handler table, and refreshes the named-route
        reverse map. Unlike `add_route`, the merge paths commit per method
        rather than two-pass-atomically across all methods.
        """
        existing = handler_table.get(method)
        if existing is not None and not self._allow_duplicate(existing, route_info):
            self._on_duplicate_route(full_path, method, existing, route_info)
            self._drop_replaced_route_name(existing, source_name, handler_table, method)
        handler_table[method] = route_info
        self._named_routes[source_name] = (full_path, param_names)
        self._reverse_converters.pop(source_name, None)

    def _finalize_plans(
        self,
        route_info: RouteInfo,
        *,
        is_ws: bool,
        reuse_handler_plan: Any = None,
    ) -> None:
        """Attach the handler/dependency plans and dispatch classification.

        On registration (`add_route`) the handler plan is built fresh; on a
        merge the parent's plan is reused via `reuse_handler_plan`. Either way
        the route-dependency plans and the trivial / request-only dispatch
        flags are recomputed here so all three registration paths stay
        byte-for-byte identical.

        Registration-time only; nothing here runs per request.
        """
        if reuse_handler_plan is not None:
            route_info.handler_plan = reuse_handler_plan
        else:
            route_info.handler_plan = build_plan(route_info.handler, websocket=is_ws)
        route_info.route_dep_plans = build_route_dep_plans(route_info.dependencies, websocket=is_ws)
        slots = route_info.handler_plan.slots
        has_deps = bool(route_info.route_dep_plans)
        # A handler with no parameter slots and no route-level dependencies
        # needs nothing resolved.
        route_info.is_trivial_plan = not slots and not has_deps
        # Request-only fast path: the handler takes only `request` and the
        # route has no dependencies. Skip DependencyResolver entirely and bind
        # kwargs = {"request": request} directly.
        route_info.is_request_only_plan = (
            len(slots) == 1 and slots[0].kind == K_REQUEST and not has_deps
        )
        if route_info.is_request_only_plan:
            route_info.request_param_name = slots[0].name
        # Straight-line dispatch eligibility: an async handler with a trivial or
        # request-only plan and none of the per-route features the fast path
        # cannot honour. WebSocket routes never enter HTTP dispatch, so they are
        # excluded outright.
        route_info.is_fast_eligible = (
            not is_ws
            and route_info.handler_plan.is_coro
            and (route_info.is_trivial_plan or route_info.is_request_only_plan)
            and route_info.response_model is None
            and route_info.response_class is None
            and route_info.status_code == HTTP_200_OK
            and route_info.subdomain is None
            and route_info.host is None
            and not route_info.defaults
            and route_info.excluded_middleware is None
        )

    def add_route(
        self,
        path: Annotated[
            str,
            Doc("URL path template, including `{param}` / `{param:converter}` placeholders."),
        ],
        handler: Annotated[
            RouteHandler,
            Doc("Async or sync callable invoked when the route matches."),
        ],
        methods: Annotated[
            list[str],
            Doc("HTTP methods this handler serves (uppercased internally)."),
        ],
        dependencies: Annotated[
            list[Any] | None,
            Doc("Dependencies run for this route, appended after the router-level ones."),
        ] = None,
        response_model: Annotated[
            Any,
            _DOC_RESPONSE_MODEL,
        ] = _INFER_RESPONSE_MODEL,
        tags: Annotated[
            list[str] | None,
            Doc("OpenAPI tags for this route, combined with the router-level tags."),
        ] = None,
        summary: Annotated[
            str | None,
            Doc("Short OpenAPI summary for this operation."),
        ] = None,
        name: Annotated[
            str | None,
            Doc("Endpoint name for `url_for` reverse lookup; defaults to the handler's name."),
        ] = None,
        description: Annotated[
            str | None,
            Doc("OpenAPI description; defaults to the handler's docstring."),
        ] = None,
        deprecated: Annotated[
            bool,
            Doc("Mark the operation as deprecated in the OpenAPI document."),
        ] = False,
        response_description: Annotated[
            str,
            Doc("Description of the successful response in the OpenAPI document."),
        ] = MSG_SUCCESSFUL_RESPONSE,
        status_code: Annotated[
            int,
            Doc("Default HTTP status code for a successful response."),
        ] = HTTP_200_OK,
        response_class: Annotated[
            Any,
            Doc("Response class for this route, overriding the router and framework defaults."),
        ] = None,
        response_model_include: Annotated[
            set[str] | None,
            Doc("Fields to include when serializing the response model."),
        ] = None,
        response_model_exclude: Annotated[
            set[str] | None,
            Doc("Fields to exclude when serializing the response model."),
        ] = None,
        response_model_exclude_unset: Annotated[
            bool,
            Doc("Omit fields left unset on the response model from the serialized output."),
        ] = False,
        response_model_exclude_defaults: Annotated[
            bool,
            Doc(
                "Omit fields equal to their default on the response model from the serialized output."
            ),
        ] = False,
        response_model_by_alias: Annotated[
            bool,
            Doc("Serialize the response model using field aliases instead of attribute names."),
        ] = False,
        response_model_exclude_none: Annotated[
            bool,
            Doc("Omit fields whose value is `None` from the serialized response model."),
        ] = False,
        include_in_schema: Annotated[
            bool,
            Doc("Register the route but omit it from the generated OpenAPI document when False."),
        ] = True,
        responses: Annotated[
            dict[int, dict[str, Any]] | None,
            Doc("Additional OpenAPI responses for this route, overlaid on the router-level ones."),
        ] = None,
        operation_id: Annotated[
            str | None,
            Doc("Explicit OpenAPI `operationId`; defaults to the route name."),
        ] = None,
        openapi_extra: Annotated[
            dict[str, Any] | None,
            Doc("Arbitrary dict deep-merged into this route's OpenAPI operation object."),
        ] = None,
        defaults: Annotated[
            dict[str, Any] | None,
            Doc(
                "Fixed values merged into the path params at dispatch without overriding URL-matched ones."
            ),
        ] = None,
        callbacks: Annotated[
            dict[str, Any] | None,
            Doc(
                "OpenAPI Callback objects emitted verbatim into the operation's `callbacks` field."
            ),
        ] = None,
        strict_slashes: Annotated[
            bool | None,
            Doc(
                "When False, match both slashed and unslashed forms; `None` defers to the app policy."
            ),
        ] = None,
        subdomain: Annotated[
            str | None,
            Doc(
                "Constrain the route to a subdomain of `SERVER_NAME`; `*` matches any non-apex subdomain."
            ),
        ] = None,
        host: Annotated[
            str | None,
            Doc("Constrain the route to an exact `Host` header value (case-insensitive)."),
        ] = None,
        expose_as_mcp_tool: Annotated[
            bool,
            Doc("Expose the route as an MCP tool in the contrib MCP registry."),
        ] = False,
        mcp_description: Annotated[
            str | None,
            Doc("LLM-facing description for the route's MCP tool, required when exposed as one."),
        ] = None,
        expose_as_mcp_resource: Annotated[
            bool,
            Doc("Expose the read-only route as an MCP resource in the contrib MCP registry."),
        ] = False,
        mcp_resource_uri: Annotated[
            str | None,
            _DOC_MCP_RESOURCE_URI,
        ] = None,
        mcp_resource_mime_type: Annotated[
            str | None,
            _DOC_MCP_RESOURCE_MIME_TYPE,
        ] = None,
        mcp_meta: Annotated[
            dict[str, Any] | None,
            _DOC_MCP_META,
        ] = None,
        mcp_resource_size: Annotated[
            int | None,
            Doc("Size in bytes advertised for the route's MCP resource."),
        ] = None,
        mcp_resource_annotations: Annotated[
            dict[str, Any] | None,
            Doc("Annotations (audience, priority) advertised for the route's MCP resource."),
        ] = None,
        mcp_scopes: Annotated[
            Sequence[str] | None,
            Doc("Authorization scopes required to call this route over MCP."),
        ] = None,
        mcp_icons: Annotated[
            Sequence[Any] | None,
            Doc("Optional MCP `Icon` objects a client may render next to the tool/resource."),
        ] = None,
        mcp_task_support: Annotated[
            bool,
            _DOC_MCP_TASK_SUPPORT,
        ] = False,
        exclude_middleware: Annotated[
            Sequence[str | type] | None,
            _DOC_EXCLUDE_MIDDLEWARE,
        ] = None,
        stream: Annotated[
            bool,
            _DOC_STREAM,
        ] = False,
    ) -> None:
        """Register a route in the radix tree.

        `strict_slashes=False` matches both the slashed and unslashed
        forms without redirecting. `None` (default)
        defers to the app's global `redirect_slashes` policy.

        `subdomain="api"` constrains the route to requests whose `Host`
        header matches `{subdomain}.{app.config["SERVER_NAME"]}`. The
        match is exact (no globbing); for wildcard subdomain matching
        use `subdomain="*"` and inspect `request.subdomain` inside the
        handler.
        """
        full_path = self.prefix + path
        # Reject a duplicate path parameter before building any tree node or
        # regex, so both the radix and regex branches get one clear error.
        _check_duplicate_params(full_path)
        node, regex_route, param_names = self._classify_route_path(full_path, strict_slashes)

        route_name = name or handler.__name__
        # Merge router-level dependencies (registered at Router.__init__)
        # with the route-specific list. Router-level dependencies run
        # first (matches the documented semantics - outer scope before inner).
        combined_deps = list(self.router_dependencies)
        if dependencies:
            combined_deps.extend(dependencies)
        # A handler that declares its response type in the return annotation gets
        # that model as its contract, so the annotation is enforced rather than
        # merely advisory: it filters the handler's return and documents the
        # response. An explicit `response_model=` always wins, and an explicit
        # `None` opts out. An annotation naming no model (a `Response` subclass,
        # `Any`, a bare `dict`) resolves to `None` and declares no contract.
        if response_model is _INFER_RESPONSE_MODEL:
            response_model = resolve_response_contract(handler)

        route_info = RouteInfo(
            handler=handler,
            param_names=param_names,
            dependencies=combined_deps if combined_deps else None,
            response_model=response_model,
            tags=(tags or []) + self.tags,
            summary=summary,
            name=route_name,
            path_template=full_path,
            description=description,
            deprecated=deprecated,
            response_description=response_description,
            status_code=status_code,
            response_class=response_class or self.default_response_class,
            response_model_include=response_model_include,
            response_model_exclude=response_model_exclude,
            response_model_exclude_unset=response_model_exclude_unset,
            response_model_exclude_defaults=response_model_exclude_defaults,
            response_model_by_alias=response_model_by_alias,
            response_model_exclude_none=response_model_exclude_none,
            include_in_schema=include_in_schema,
            responses=(
                None
                if not self.router_responses and not responses
                else {**self.router_responses, **(responses or {})}
            ),
            operation_id=operation_id,
            openapi_extra=openapi_extra,
            defaults=defaults,
            callbacks=callbacks,
            subdomain=subdomain,
            host=host,
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
            excluded_middleware=_split_exclusions(exclude_middleware),
        )
        route_info.stream = stream
        route_info.strict_slashes = strict_slashes

        # Pre-compute the handler resolution plan once, here at registration.
        # A WebSocket route's plan is built in websocket mode so the
        # `WebSocket` connection is bound by annotation / name and its
        # dependency graph runs through the shared resolver.
        is_ws = any(m.upper() == ROUTE_METHOD_WEBSOCKET for m in methods)
        self._finalize_plans(route_info, is_ws=is_ws)

        # `node` is the radix leaf for tree routes; `regex_route` is set
        # instead for regex routes (the two branches above are mutually
        # exclusive, so exactly one of them holds the handler table).
        if regex_route is not None:
            handler_table = regex_route.handlers
        else:
            assert node is not None
            handler_table = node.handlers
        self._commit_route_methods(methods, handler_table, full_path, route_info, route_name)

        # Register the named route for url_for only once the route is committed
        # to the handler table above. The reverse entry reflects the route that
        # actually wins on the override/warn replace paths, and is never written
        # if the duplicate policy raised. Drop any stale reverse-converter cache
        # so a re-registered name re-derives from its new template.
        # A name taken by a *different* path is a collision: the earlier route
        # keeps serving but becomes unreachable by name, so `url_for` starts
        # resolving to somewhere else and a template renders the wrong link.
        # `on_duplicate` governs method+path and never saw this. Re-registering
        # the *same* path is the override path, where the name legitimately
        # moves with the route it names, so that stays silent.
        previous = self._named_routes.get(route_name)
        if previous is not None and previous[0] != full_path:
            _logger.warning(
                "Duplicate route name %r: %s now resolves to %s, and %s is no "
                "longer reachable by name",
                route_name,
                route_name,
                full_path,
                previous[0],
            )
        self._named_routes[route_name] = (full_path, param_names)
        self._reverse_converters.pop(route_name, None)

    def _get_or_create_regex_route(self, full_path: str) -> tuple[RegexRoute, list[str]]:
        """Return the indexed regex route for a path, building and indexing it once."""
        target = self._regex_route_index.get(full_path)
        if target is None:
            pattern = build_route_regex(full_path)
            param_names = list(pattern.groupindex)
            target = RegexRoute(full_path, pattern, param_names)
            self._regex_routes.append(target)
            self._regex_route_index[full_path] = target
        else:
            param_names = target.param_names
        return target, param_names

    def _classify_route_path(
        self, full_path: str, strict_slashes: bool | None
    ) -> tuple[RadixNode | None, RegexRoute | None, list[str]]:
        """Place a path on the regex fallback or the radix tree.

        A path the radix tree cannot express (partial-segment params, multi-brace
        segments, raw regex converters, greedy `:path` with a suffix) is compiled
        to a `RegexRoute`; everything else is inserted into the tree. Return the
        radix leaf (or `None` for a regex route), the `RegexRoute` (or `None` for
        a tree route), and the ordered parameter names - exactly one of the two
        node results is non-`None`.
        """
        has_trailing_slash = full_path.endswith("/") and full_path != "/"
        if is_regex_path(full_path):
            regex_route, param_names = self._get_or_create_regex_route(full_path)
            if strict_slashes is False:
                regex_route.tolerant_slash = True
            return None, regex_route, param_names

        segments = self._split_path(full_path)
        node, param_names = self._insert_path_into_tree(self._root, segments, full_path)
        # `/foo` and `/foo/` share this node. Record which form was registered
        # without clearing the other, so registering one variant never flips the
        # already-registered variant's slash strictness.
        if has_trailing_slash:
            node.trailing_slash = True
        else:
            node.unslashed_variant = True
        if strict_slashes is False:
            node.tolerant_slash = True
        return node, None, param_names

    def _commit_route_methods(
        self,
        methods: list[str],
        handler_table: dict[str, RouteInfo],
        full_path: str,
        route_info: RouteInfo,
        route_name: str,
    ) -> None:
        """Commit a route's methods into the handler table atomically.

        Two-pass so a multi-method registration is all-or-nothing. Pass 1
        evaluates the duplicate policy for every method and raises
        `DuplicateRouteError` before any mutation, so a collision on a later verb
        leaves the router fully unchanged (the caller catches the error expecting
        an untouched router). Pass 2 drops any displaced reverse entries, then
        writes the handlers. The named-route reverse entry is written by the
        caller only after this returns, so a raised policy never pollutes
        `url_for`.
        """
        replaceable: list[tuple[str, RouteInfo]] = []
        for method in methods:
            mkey = method.upper()
            existing = handler_table.get(mkey)
            if existing is not None and not self._allow_duplicate(existing, route_info):
                # Nothing has been mutated yet, so a raise here leaves the router
                # unchanged.
                self._on_duplicate_route(full_path, mkey, existing, route_info)
                # `warn` and `override` allow the replace, so remember the
                # displaced route: pass 2 drops its reverse entry after the check.
                replaceable.append((mkey, existing))
        for mkey, existing in replaceable:
            # Drop the displaced route's reverse entry when it had a different
            # name, so url_for(old_name) stops resolving to a dead route.
            self._drop_replaced_route_name(existing, route_name, handler_table, mkey)
        for method in methods:
            handler_table[method.upper()] = route_info

    # ── Matching ──────────────────────────────────────────

    @staticmethod
    def _regex_route_match(route: RegexRoute, path: str) -> re.Match[str] | None:
        """Match `path` against a regex route, honoring `tolerant_slash`.

        When the route was registered with `strict_slashes=False`, the
        slashed and unslashed forms both match - mirroring the radix tree's
        `tolerant_slash` behaviour.
        """
        m = route.pattern.match(path)
        if m is not None:
            return m
        if route.tolerant_slash:
            toggled = path[:-1] if path.endswith("/") and path != "/" else path + "/"
            return route.pattern.match(toggled)
        return None

    @staticmethod
    def _coerce_regex_params(route: RegexRoute, m: re.Match[str]) -> dict[str, Any] | None:
        """Apply each placeholder's built-in converter to the matched groups.

        Built-in specs (`int`, `float`, `uuid`, `path`, `any(...)`) coerce to
        the same Python types the radix tree produces; bare and raw-regex
        groups have no converter and stay as strings. A built-in converter
        enforces guards the regex fragment alone does not - `int`'s digit cap,
        for instance, rejects a 21-digit value that `-?\\d+` happily matches.
        When a converter rejects its group, the regex route is treated as a
        miss (return `None`) so the same input is rejected on a regex route as
        on the equivalent radix route, instead of leaking through as a string.
        """
        params = m.groupdict()
        converters = route.converters
        if not converters:
            return params
        for name, value in params.items():
            conv = converters.get(name)
            if conv is None:
                continue
            ok, coerced = conv.match(value)
            if not ok:
                return None
            params[name] = coerced
        return params

    def _match_regex(self, method: str, path: str) -> RouteMatch | None:
        """Try the regex fallback routes in registration order.

        Called only on a radix miss and only when regex routes exist. The
        first route whose pattern fully matches and whose handlers include
        the method (with the same HEAD->GET fallback as the tree) wins.
        Matched groups are coerced via each placeholder's built-in converter
        so regex-route params are typed exactly like radix-route params.
        """
        method_upper = method if method.isupper() else method.upper()
        for route in self._regex_routes:
            m = self._regex_route_match(route, path)
            if m is None:
                continue
            info = route.handlers.get(method_upper)
            if info is None and method_upper == HTTP_METHOD_HEAD:
                info = route.handlers.get(HTTP_METHOD_GET)
            if info is None:
                continue
            params = self._coerce_regex_params(route, m)
            if params is None:
                # A built-in converter rejected its matched group (e.g. an
                # over-long `:int`). Treat it as a miss and try the next route.
                continue
            return RouteMatch(route_info=info, path_params=params)
        return None

    def _build_static_routes(self) -> dict[tuple[str, str], RouteInfo]:
        """Map (method, exact-path) -> RouteInfo for literal, strict paths.

        A literal path resolves to a node reached purely through static segments,
        so its match is fixed at registration. A node's param/wildcard children
        only ever match *longer* paths, so they do not affect the node's own
        handler for the exact path - only the slash-redirect flags do. Nodes with
        `trailing_slash` or `tolerant_slash` are therefore excluded and fall
        through to the tree, which keeps the exact slash semantics. Methods are
        stored uppercase (as in `RadixNode.handlers`), so a lowercase request
        misses here and resolves on the tree. A GET-only literal also gets a
        HEAD alias to its GET RouteInfo (RFC 9110 Sec. 9.3.2), so HEAD requests
        resolve here rather than re-running the tree's HEAD->GET fallback.
        """
        smap: dict[tuple[str, str], RouteInfo] = {}
        stack: list[tuple[RadixNode, str]] = [(self._root, "")]
        while stack:
            node, prefix = stack.pop()
            if node.handlers and not node.trailing_slash and not node.tolerant_slash:
                path = prefix or "/"
                for method, info in node.handlers.items():
                    smap[(method, path)] = info
                # RFC 9110 Sec. 9.3.2: HEAD falls back to GET. The body strip
                # is transport-level (the ASGI/native emit checks the request
                # method), so aliasing HEAD to the GET RouteInfo here is
                # behaviorally identical to the tree's per-request fallback and
                # spares HEAD requests the split + tree walk.
                if HTTP_METHOD_HEAD not in node.handlers:
                    get_info = node.handlers.get(HTTP_METHOD_GET)
                    if get_info is not None:
                        smap[(HTTP_METHOD_HEAD, path)] = get_info
            for seg, child in node.static_children.items():
                stack.append((child, prefix + "/" + seg))
        return smap

    def match(self, method: str, path: str) -> RouteMatch | None:
        """Match a request path. Static map, then radix tree, then regex.

        O(1) for a literal path (the static map), else O(k) on the tree where
        k = path depth. The regex fallback runs only when the tree misses
        **and** regex routes are registered; the tree always wins over regex
        when both could match.
        """
        smap = self._static_routes
        if smap is None:
            smap = self._static_routes = self._build_static_routes()
        info = smap.get((method, path))
        if info is not None:
            return RouteMatch(route_info=info, path_params={})
        match = self._match_tree(method, path)
        if match is not None:
            return match
        # Zero cost when no regex route is registered: the guard short-circuits
        # before touching the (empty) list.
        if self._regex_routes:
            return self._match_regex(method, path)
        return None

    def _match_tree(self, method: str, path: str) -> RouteMatch | None:
        """Match against the radix tree alone. O(k) where k = path depth."""
        segments = self._split_path(path)
        request_has_slash = path.endswith("/") and path != "/"
        params: dict[str, str] = {}
        result = self._match_node(self._root, segments, 0, params)
        if result is None:
            return None

        # Trailing slash strictness: a route registered with slash only matches
        # slashed requests, and vice versa. `tolerant_slash` (per-route
        # `strict_slashes=False`) skips this gate. When both the slashed and
        # unslashed forms were registered, the node serves both shapes, so
        # neither gate fires.
        if _slash_mismatch(result, request_has_slash):
            return None

        # Handlers are stored uppercase - RFC-conforming clients send the
        # method already uppercased, so try the raw form first and only
        # uppercase on miss. `.isupper()` is the right guard: CPython does
        # not promise identity for `str.upper()` even when the input is
        # already uppercase, so an `is`-based shortcut would be a lie.
        handler_info = result.handlers.get(method)
        if handler_info is None:
            if method.isupper():
                method_upper = method
            else:
                method_upper = method.upper()
                handler_info = result.handlers.get(method_upper)
            # RFC 9110 Sec. 9.3.2: HEAD falls back to GET; the dispatcher
            # strips the body on the way out.
            if handler_info is None and method_upper == HTTP_METHOD_HEAD:
                handler_info = result.handlers.get(HTTP_METHOD_GET)
            if handler_info is None:
                return None

        return RouteMatch(route_info=handler_info, path_params=params)

    def _match_node(
        self,
        node: RadixNode,
        segments: tuple[str, ...],
        idx: int,
        params: dict[str, Any],
    ) -> RadixNode | None:
        """Recursive radix tree traversal with per-converter validation."""
        # Flatten static-only descent - when the current node has no
        # alternative branches to backtrack into, recursing per static
        # segment burns one Python frame per hop for no gain.
        seg_count = len(segments)
        while idx < seg_count and not node.param_children and node.wildcard_child is None:
            static_child = node.static_children.get(segments[idx])
            if static_child is None:
                return None
            node = static_child
            idx += 1

        if idx == seg_count:
            return node if node.handlers else None

        seg = segments[idx]

        # Try static match first (fastest path) - O(1) dict lookup. We can
        # still get here when alternative param/wildcard branches exist on
        # this node, so the recursion preserves backtracking semantics.
        static_child = node.static_children.get(seg)
        if static_child is not None:
            result = self._match_node(static_child, segments, idx + 1, params)
            if result is not None:
                return result

        # Try param match - each candidate validates the segment via its
        # converter. Greedy converters (path) consume the remainder in one go.
        # When this node has exactly one param child, a failed inner match
        # has no alternative to back off to, so the rollback `del` is moot.
        # Invariant: `params` is owned by the top-level `match()` call and
        # discarded when traversal returns None, so any leaked key is
        # confined to that throwaway dict. Sharing `params` across sibling
        # subtrees in a future refactor would break this assumption.
        param_children = node.param_children
        single_param = len(param_children) == 1
        for child in param_children:
            converter = child.converter
            # Bind the param name once so the assignment branch and the gated
            # rollback below share a single attribute load.
            pname = child.param_name
            if converter.greedy:
                rest = "/".join(segments[idx:])
                ok, coerced = converter.match(rest)
                if ok and child.handlers:
                    params[pname] = coerced
                    return child
                continue
            ok, coerced = converter.match(seg)
            if not ok:
                continue
            params[pname] = coerced
            result = self._match_node(child, segments, idx + 1, params)
            if result is not None:
                return result
            if not single_param:
                del params[pname]

        # Try wildcard (legacy `*` syntax - kept for back-compat).
        if node.wildcard_child is not None:
            params["_wildcard"] = "/".join(segments[idx:])
            return node.wildcard_child

        return None

    def get_allowed_methods(self, path: str) -> list[str]:
        """Get allowed methods for a path (for 405 responses).

        Unions the methods reachable through the radix tree AND any regex
        routes that match the same path, so a path served by a tree handler on
        one method and a regex handler on another reports both for 405/OPTIONS.
        Tree methods are listed first (dispatch precedence); duplicates removed.
        """
        segments = self._split_path(path)
        request_has_slash = path.endswith("/") and path != "/"
        params: dict[str, str] = {}
        # Ordered set: tree methods first, then regex, deduped.
        methods: dict[str, None] = {}
        node = self._match_node(self._root, segments, 0, params)
        # Respect trailing-slash strictness through the same predicate the match
        # path uses, so `Allow` cannot advertise a method that would not match.
        if node is not None and node.handlers and not _slash_mismatch(node, request_has_slash):
            methods.update(dict.fromkeys(node.handlers))
        if self._regex_routes:
            for route in self._regex_routes:
                m = self._regex_route_match(route, path)
                if m is None:
                    continue
                # A converter rejection (e.g. an over-long `:int`) is a full
                # miss, not a method mismatch - keep it a 404, never a 405.
                if self._coerce_regex_params(route, m) is None:
                    continue
                methods.update(dict.fromkeys(route.handlers))
        return list(methods)

    # ── Decorator API ─────────────────────────────────────

    def route(
        self,
        path: Annotated[
            str,
            Doc("URL path template, including `{param}` / `{param:converter}` placeholders."),
        ],
        methods: Annotated[
            list[str] | None,
            Doc("HTTP methods this handler serves; defaults to `GET`."),
        ] = None,
        dependencies: Annotated[
            list[Any] | None,
            Doc("Dependencies run for this route, appended after the router-level ones."),
        ] = None,
        response_model: Annotated[
            Any,
            _DOC_RESPONSE_MODEL,
        ] = _INFER_RESPONSE_MODEL,
        tags: Annotated[
            list[str] | None,
            Doc("OpenAPI tags for this route, combined with the router-level tags."),
        ] = None,
        summary: Annotated[
            str | None,
            Doc("Short OpenAPI summary for this operation."),
        ] = None,
        name: Annotated[
            str | None,
            Doc("Endpoint name for `url_for` reverse lookup; defaults to the handler's name."),
        ] = None,
        description: Annotated[
            str | None,
            Doc("OpenAPI description; defaults to the handler's docstring."),
        ] = None,
        deprecated: Annotated[
            bool,
            Doc("Mark the operation as deprecated in the OpenAPI document."),
        ] = False,
        response_description: Annotated[
            str,
            Doc("Description of the successful response in the OpenAPI document."),
        ] = MSG_SUCCESSFUL_RESPONSE,
        status_code: Annotated[
            int,
            Doc("Default HTTP status code for a successful response."),
        ] = HTTP_200_OK,
        response_class: Annotated[
            Any,
            Doc("Response class for this route, overriding the router and framework defaults."),
        ] = None,
        response_model_include: Annotated[
            set[str] | None,
            Doc("Fields to include when serializing the response model."),
        ] = None,
        response_model_exclude: Annotated[
            set[str] | None,
            Doc("Fields to exclude when serializing the response model."),
        ] = None,
        response_model_exclude_unset: Annotated[
            bool,
            Doc("Omit fields left unset on the response model from the serialized output."),
        ] = False,
        response_model_exclude_defaults: Annotated[
            bool,
            Doc(
                "Omit fields equal to their default on the response model from the serialized output."
            ),
        ] = False,
        response_model_by_alias: Annotated[
            bool,
            Doc("Serialize the response model using field aliases instead of attribute names."),
        ] = False,
        response_model_exclude_none: Annotated[
            bool,
            Doc("Omit fields whose value is `None` from the serialized response model."),
        ] = False,
        include_in_schema: Annotated[
            bool,
            Doc("Register the route but omit it from the generated OpenAPI document when False."),
        ] = True,
        responses: Annotated[
            dict[int, dict[str, Any]] | None,
            Doc("Additional OpenAPI responses for this route, overlaid on the router-level ones."),
        ] = None,
        operation_id: Annotated[
            str | None,
            Doc("Explicit OpenAPI `operationId`; defaults to the route name."),
        ] = None,
        openapi_extra: Annotated[
            dict[str, Any] | None,
            Doc("Arbitrary dict deep-merged into this route's OpenAPI operation object."),
        ] = None,
        defaults: Annotated[
            dict[str, Any] | None,
            Doc(
                "Fixed values merged into the path params at dispatch without overriding URL-matched ones."
            ),
        ] = None,
        callbacks: Annotated[
            dict[str, Any] | None,
            Doc(
                "OpenAPI Callback objects emitted verbatim into the operation's `callbacks` field."
            ),
        ] = None,
        strict_slashes: Annotated[
            bool | None,
            Doc(
                "When False, match both slashed and unslashed forms; `None` defers to the app policy."
            ),
        ] = None,
        subdomain: Annotated[
            str | None,
            Doc(
                "Constrain the route to a subdomain of `SERVER_NAME`; `*` matches any non-apex subdomain."
            ),
        ] = None,
        host: Annotated[
            str | None,
            Doc("Constrain the route to an exact `Host` header value (case-insensitive)."),
        ] = None,
        expose_as_mcp_tool: Annotated[
            bool,
            Doc("Expose the route as an MCP tool in the contrib MCP registry."),
        ] = False,
        mcp_description: Annotated[
            str | None,
            Doc("LLM-facing description for the route's MCP tool, required when exposed as one."),
        ] = None,
        expose_as_mcp_resource: Annotated[
            bool,
            Doc("Expose the read-only route as an MCP resource in the contrib MCP registry."),
        ] = False,
        mcp_resource_uri: Annotated[
            str | None,
            _DOC_MCP_RESOURCE_URI,
        ] = None,
        mcp_resource_mime_type: Annotated[
            str | None,
            _DOC_MCP_RESOURCE_MIME_TYPE,
        ] = None,
        mcp_meta: Annotated[
            dict[str, Any] | None,
            _DOC_MCP_META,
        ] = None,
        mcp_resource_size: Annotated[
            int | None,
            Doc("Size in bytes advertised for the route's MCP resource."),
        ] = None,
        mcp_resource_annotations: Annotated[
            dict[str, Any] | None,
            Doc("Annotations (audience, priority) advertised for the route's MCP resource."),
        ] = None,
        mcp_scopes: Annotated[
            Sequence[str] | None,
            Doc("Authorization scopes required to call this route over MCP."),
        ] = None,
        mcp_icons: Annotated[
            Sequence[Any] | None,
            Doc("Optional MCP `Icon` objects a client may render next to the tool/resource."),
        ] = None,
        mcp_task_support: Annotated[
            bool,
            _DOC_MCP_TASK_SUPPORT,
        ] = False,
        exclude_middleware: Annotated[
            Sequence[str | type] | None,
            _DOC_EXCLUDE_MIDDLEWARE,
        ] = None,
        stream: Annotated[
            bool,
            _DOC_STREAM,
        ] = False,
    ) -> Callable:
        """Register a route for any set of HTTP methods.

        `exclude_middleware=["CSRFMiddleware"]` opts this route out of the
        named middleware (matched against each middleware's `name`), so a
        webhook or health-check route can skip CSRF, auth, or rate limiting
        without forking the middleware. Routes that declare no exclusions
        pay no extra per-request cost.
        """

        def decorator(func: RouteHandler) -> RouteHandler:
            """Register `func` for the route and return it unchanged."""
            self.add_route(
                path=path,
                handler=func,
                methods=methods or [HTTP_METHOD_GET],
                dependencies=dependencies,
                response_model=response_model,
                tags=tags,
                summary=summary,
                name=name,
                description=description,
                deprecated=deprecated,
                response_description=response_description,
                status_code=status_code,
                response_class=response_class,
                response_model_include=response_model_include,
                response_model_exclude=response_model_exclude,
                response_model_exclude_unset=response_model_exclude_unset,
                response_model_exclude_defaults=response_model_exclude_defaults,
                response_model_by_alias=response_model_by_alias,
                response_model_exclude_none=response_model_exclude_none,
                include_in_schema=include_in_schema,
                responses=responses,
                operation_id=operation_id,
                openapi_extra=openapi_extra,
                defaults=defaults,
                callbacks=callbacks,
                strict_slashes=strict_slashes,
                subdomain=subdomain,
                host=host,
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
                exclude_middleware=exclude_middleware,
                stream=stream,
            )
            return func

        return decorator

    def get(self, path: str, **kwargs: Any) -> Callable:
        """`GET` route decorator. Safe and idempotent - RFC 9110 Sec. 9.3.1."""
        return self.route(path, methods=[HTTP_METHOD_GET], **kwargs)

    def post(self, path: str, **kwargs: Any) -> Callable:
        """`POST` route decorator - RFC 9110 Sec. 9.3.3."""
        return self.route(path, methods=[HTTP_METHOD_POST], **kwargs)

    def put(self, path: str, **kwargs: Any) -> Callable:
        """`PUT` route decorator. Idempotent - RFC 9110 Sec. 9.3.4."""
        return self.route(path, methods=[HTTP_METHOD_PUT], **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Callable:
        """`PATCH` route decorator - RFC 5789."""
        return self.route(path, methods=[HTTP_METHOD_PATCH], **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Callable:
        """`DELETE` route decorator. Idempotent - RFC 9110 Sec. 9.3.5."""
        return self.route(path, methods=[HTTP_METHOD_DELETE], **kwargs)

    def head(self, path: str, **kwargs: Any) -> Callable:
        """`HEAD` route decorator. Like `GET` with no body - RFC 9110 Sec. 9.3.2."""
        return self.route(path, methods=[HTTP_METHOD_HEAD], **kwargs)

    def options(self, path: str, **kwargs: Any) -> Callable:
        """`OPTIONS` route decorator - RFC 9110 Sec. 9.3.7."""
        return self.route(path, methods=[HTTP_METHOD_OPTIONS], **kwargs)

    def trace(self, path: str, **kwargs: Any) -> Callable:
        """`TRACE` route decorator - RFC 9110 Sec. 9.3.8."""
        return self.route(path, methods=[HTTP_METHOD_TRACE], **kwargs)

    def query(self, path: str, **kwargs: Any) -> Callable:
        """`QUERY` route decorator - RFC 10008.

        QUERY is safe and idempotent like GET but carries a request body like
        POST, for read-only operations whose parameters do not fit a URL (search,
        filtering, paging). The handler reads the body exactly as a POST handler
        does (`request.get_json()` / a body model parameter).
        """
        return self.route(path, methods=[HTTP_METHOD_QUERY], **kwargs)

    def websocket(
        self,
        path: Annotated[
            str,
            Doc("URL path template for the WebSocket route, including `{param}` placeholders."),
        ],
    ) -> Callable:
        """Register a WebSocket route via decorator."""

        def decorator(func: RouteHandler) -> RouteHandler:
            self.add_route(path=path, handler=func, methods=[ROUTE_METHOD_WEBSOCKET])
            return func

        return decorator

    # `websocket_route` is an alias for the `websocket` decorator.
    websocket_route = websocket

    def websocket_listener(
        self,
        path: str,
        *,
        receive: str = "json",
        send: str = "json",
        on_connect: RouteHandler | Callable[..., Any] | None = None,
        on_disconnect: RouteHandler | Callable[..., Any] | None = None,
    ) -> Callable:
        """Register a WebSocket route wrapping a per-message callback.

        The decorated callback handles one message at a time; the framework
        owns the accept handshake, the receive loop, and the clean close on
        disconnect. The callback is called as `cb(data)`, or `cb(ws, data)`
        when its first parameter is named `ws`/`socket` (or it takes two
        positional parameters). Returning a non-`None` value sends it back in
        `send` mode; returning `None` sends nothing.

        `receive`/`send` select the codec (`"json"` default, or `"text"` /
        `"bytes"`). `on_connect(ws)` runs after accept; `on_disconnect(ws)`
        always runs when the loop ends, including on peer disconnect. Sync
        callbacks and hooks are offloaded to the executor.

        Usage::

            @app.websocket_listener("/echo")
            async def echo(data):
                return data

        For full control over the handshake and loop use `@app.websocket`.
        """

        def decorator(func: RouteHandler | Callable[..., Any]) -> RouteHandler | Callable[..., Any]:
            """Build the listener handler, register it, and return `func`."""
            handler = build_listener_handler(
                func,
                receive=receive,
                send=send,
                on_connect=on_connect,
                on_disconnect=on_disconnect,
            )
            self.add_route(path=path, handler=handler, methods=[ROUTE_METHOD_WEBSOCKET])
            return func

        return decorator

    def add_websocket_route(
        self,
        path: Annotated[
            str,
            Doc("URL path template for the WebSocket route, including `{param}` placeholders."),
        ],
        handler: Annotated[
            RouteHandler,
            Doc("Callable invoked with the accepted WebSocket connection when the route matches."),
        ],
    ) -> None:
        """Register a WebSocket route imperatively (ASGI shape).

        The non-decorator form of `@app.websocket(path)`.
        """
        self.add_route(path=path, handler=handler, methods=[ROUTE_METHOD_WEBSOCKET])

    def add_api_websocket_route(
        self, path: str, endpoint: RouteHandler, name: str | None = None
    ) -> None:
        """Register an imperative WebSocket route, mirroring `add_api_route`.

        The non-decorator form of `@app.websocket(path)`. `name`, when given,
        registers the route for reverse lookup so `app.url_for(name)` resolves
        to its path.
        """
        self.add_route(path=path, handler=endpoint, methods=[ROUTE_METHOD_WEBSOCKET], name=name)

    def add_api_route(
        self,
        path: str,
        endpoint: RouteHandler,
        *,
        methods: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Register a route imperatively.

        The non-decorator form: the handler argument is named `endpoint`
        here and forwarded to `add_route` (where it is `handler`). All
        route kwargs - `response_model`, `tags`, `dependencies`,
        `status_code`, `openapi_extra`, ... - pass straight through.
        Defaults to `["GET"]` when `methods` is omitted.
        """
        self.add_route(
            path=path,
            handler=endpoint,
            methods=methods or [HTTP_METHOD_GET],
            **kwargs,
        )

    # ── Reverse URL lookup ────────────────────────────────

    def url_for(self, name: str, /, **path_params: Any) -> str:
        """Reverse URL lookup by route name (`url_for`).

        Substitutes each `{name}` placeholder in the registered template
        with the matching `path_params` kwarg. Underscore-prefixed kwargs
        are control parameters (convention):

        - `_external=True` - return an absolute URL. Uses
          `app.config["SERVER_NAME"]` when set, otherwise falls back to
          `localhost`. Caller should override `_scheme`/`_host` for
          anything more specific.
        - `_scheme="https"` - override scheme on the absolute URL.
        - `_host="example.com"` - override host on the absolute URL.
        - `_anchor="section"` - append `#section`.
        - Any other unmatched kwarg becomes a query-string parameter.
        """
        if name not in self._named_routes:
            raise ValueError(f"No route named {name!r}")

        # Pop control flags before we walk path_params.
        external = path_params.pop("_external", False)
        scheme = path_params.pop("_scheme", None)
        host = path_params.pop("_host", None)
        anchor = path_params.pop("_anchor", None)

        template, param_names = self._named_routes[name]
        consumed: set[str] = set()
        for pname in param_names:
            if pname not in path_params:
                raise ValueError(f"Missing path parameter {pname!r} for route {name!r}")
            consumed.add(pname)

        # Validate each typed param through its converter so a reversed URL is
        # guaranteed to resolve. `url_for('item', id='abc')` on `/items/{id:int}`
        # raises here instead of emitting a dead `/items/abc`. Bare `{name}` and
        # raw-regex params have no validating converter and are skipped.
        converters = self._reverse_converters.get(name)
        if converters is None:
            converters = _reverse_converters_for(template)
            self._reverse_converters[name] = converters
        for pname, converter in converters.items():
            value = path_params[pname]
            text = str(value)
            # A segment-bounded converter never sees a `/` when matching - the
            # path splitter has already cut on it - so its `match` has no reason
            # to test for one, and `StringConverter` does not. Reversing did
            # test with `match` alone, so `url_for(..., name="a/b")` returned
            # `/b/a/b`, a URL this router cannot match. The slash test belongs
            # here, on the reverse path, and not in `match`: adding it there
            # would put a scan on every parameterised match to fix a URL-
            # building bug. `greedy` is the existing flag for the one converter
            # that legitimately crosses segments.
            ok, _ = converter.match(text)
            if ok and not converter.greedy and "/" in text:
                ok = False
            if not ok:
                raise ValueError(
                    f"Value {value!r} for path parameter {pname!r} is invalid for route {name!r}"
                )

        # Single-pass substitution built from template segments prevents
        # injection: a parameter value containing `{other_param}` cannot
        # corrupt later placeholders. Balance-aware so a spec with its own
        # braces (`{id:[0-9]{2}}`) is replaced whole.
        out: list[str] = []
        pos = 0
        for ph in _iter_placeholders(template):
            out.append(template[pos : ph.start])
            if ph.name in path_params:
                out.append(str(path_params[ph.name]))
            else:
                out.append(template[ph.start : ph.end])
            pos = ph.end
        out.append(template[pos:])
        path = "".join(out)

        # Anything left in path_params is a query-string parameter (the
        # behaviour). Order matches caller's kwarg order via dict insertion.
        extras = {k: v for k, v in path_params.items() if k not in consumed}
        if extras:
            path = f"{path}?{urlencode(extras, doseq=True)}"

        if anchor is not None:
            path = f"{path}#{anchor}"

        if external or scheme or host:
            # SERVER_NAME is "host[:port]"; without it, default to
            # localhost - the absolute-URL request was made outside a request
            # context where we'd otherwise know the host.
            cfg_host, cfg_scheme = self._absolute_url_defaults()
            netloc = host or cfg_host or "localhost"
            url_scheme = scheme or cfg_scheme
            return f"{url_scheme}://{netloc}{path}"

        return path

    def _absolute_url_defaults(self) -> tuple[str | None, str]:
        """Return `(host, scheme)` for an absolute URL built with no request.

        A bare `Router` has no configuration, so it has no opinion: the caller's
        explicit `host=` / `scheme=` win, and `url_for` falls back to
        `localhost` over HTTP. `Veloce` overrides this to answer from
        `SERVER_NAME` and `PREFERRED_URL_SCHEME`.

        A hook rather than `hasattr(self, "config")`, which is what this was: a
        base class testing for an attribute only its subclass defines, so the
        dependency ran the wrong way and neither type checking nor a reader
        could see the relationship.
        """
        return None, URL_SCHEME_HTTP

    # Veloce exposes this exact reverse-URL builder as `url_path_for`.
    # `url_for` is the canonical method; this is a thin
    # alias so calling code reads cleanly.
    url_path_for = url_for

    # ── Introspection and merge ───────────────────────────

    def iter_routes(self, *, include_hidden: bool = False) -> list[tuple[str, str, RouteInfo]]:
        """Return every registered route as ``(method, path, info)``.

        ``app.routes`` is a summary view: six fields of `RouteInfo`'s full
        record, which is enough to render a route table and not enough for
        anything that inspects a route. This returns the records themselves, so
        response models, dependencies, security requirements and the rest are
        reachable without touching private state.

        Hidden routes - WebSocket routes and those registered
        ``include_in_schema=False`` - are omitted unless ``include_hidden`` is
        set; the default is the schema-visible set.

        Usage::

            for method, path, info in app.iter_routes():
                if info.response_model is not None:
                    print(method, path, info.response_model)
        """
        return self._collect_all_routes(include_hidden)

    def _collect_all_routes(self, include_hidden: bool = False) -> list[tuple[str, str, RouteInfo]]:
        """Collect routes as (method, path, info) tuples.

        By default only schema-visible HTTP routes are returned (the set
        OpenAPI generation needs). Pass ``include_hidden=True`` to also get
        WebSocket routes and routes registered with ``include_in_schema=False``
        - required when re-registering a blueprint's routes onto an app, where
        every route must enter the radix tree regardless of schema visibility.
        """
        routes: list[tuple[str, str, RouteInfo]] = []
        self._walk_tree(self._root, [], routes, include_hidden)
        # Tree routes are the runtime winners (match() consults the tree first).
        # Track the (method, path-shape) pairs they own so a regex route mapping
        # to the same effective path+method does not shadow them in the schema -
        # compared by SHAPE (each `{param}` normalized to `{}`) so a tree
        # `/items/{slug}` still shadows a regex `/items/{id}` despite the
        # different parameter name. Skipped under include_hidden, where every
        # route must still be surfaced for blueprint re-registration.
        # Limitation: shape comparison treats any same-shape tree route as a
        # shadow; a tree route with a constraining converter (e.g. `{id:int}`)
        # does not in fact match every input, so a complementary regex route
        # (e.g. letters-only) is dropped from the schema here even though it is
        # reachable. Acceptable: schema omission of a rare overlapping route,
        # never a dispatch change.
        tree_owned: set[tuple[str, str]] = (
            set() if include_hidden else {(method, _path_shape(path)) for method, path, _ in routes}
        )
        # Regex fallback routes are not in the tree; surface them here so
        # OpenAPI, blueprint re-registration, and url-map building see them.
        # The exposed path is the OpenAPI-style form built from the template.
        for route in self._regex_routes:
            path = route.openapi_path
            for method, info in route.handlers.items():
                if include_hidden or (method != ROUTE_METHOD_WEBSOCKET and info.include_in_schema):
                    if not include_hidden and (method, _path_shape(path)) in tree_owned:
                        # A tree route already owns this path-shape+method and
                        # wins at dispatch; do not let the regex handler shadow
                        # it in the schema.
                        continue
                    routes.append((method, path, info))
        return routes

    def _walk_tree(
        self,
        node: RadixNode,
        path_parts: list[str],
        out: list[tuple[str, str, RouteInfo]],
        include_hidden: bool = False,
    ) -> None:
        if node.handlers:
            path = "/" + "/".join(path_parts) if path_parts else "/"
            for method, info in node.handlers.items():
                if include_hidden or (method != ROUTE_METHOD_WEBSOCKET and info.include_in_schema):
                    out.append((method, path, info))
        for child in node.static_children.values():
            self._walk_tree(child, path_parts + [child.segment], out, include_hidden)
        for child in node.param_children:
            seg = "{" + child.param_name + "}"
            self._walk_tree(child, path_parts + [seg], out, include_hidden)
        if node.wildcard_child is not None:
            self._walk_tree(node.wildcard_child, path_parts + ["{path}"], out, include_hidden)

    def include_router(self, router: Router, prefix: str = "") -> None:
        """Include another router (a sub-router with its own prefix, tags, and hooks)."""
        # Collect all routes from sub-router and re-add with combined prefix
        extra_prefix = prefix.rstrip("/")
        self._merge_node(router._root, extra_prefix, [])
        # Tree merge above only walks the radix structure; the child's regex
        # fallback routes live outside it, so merge them explicitly under the
        # same prefix (no-op when the child registered none).
        if router._regex_routes:
            self._merge_regex_routes(router, extra_prefix)

    def _merge_regex_routes(self, router: Router, prefix: str) -> None:
        """Merge a child router's regex fallback routes into this router.

        Re-prefixes each template, recompiles the anchored pattern, applies
        this router's `router_dependencies`, and preserves the route name the
        same way `_merge_node` does for tree routes.
        """
        for src in router._regex_routes:
            full_path = prefix + src.template if prefix else src.template
            target, param_names = self._get_or_create_regex_route(full_path)
            # Carry slash-tolerance from the source so a child route declared
            # with `strict_slashes=False` keeps it after merge.
            if src.tolerant_slash:
                target.tolerant_slash = True

            for method, info in src.handlers.items():
                route_info = self._build_merged_route_info(info, param_names, full_path)
                # Reuse the parent's pre-computed handler plan; route_dep_plans
                # are rebuilt from the combined dependencies since this router's
                # router_dependencies may have been prepended above.
                is_ws = method.upper() == ROUTE_METHOD_WEBSOCKET
                self._finalize_plans(route_info, is_ws=is_ws, reuse_handler_plan=info.handler_plan)
                self._commit_merged_method(
                    target.handlers, method, route_info, info.name, full_path, param_names
                )

    def _merge_node(self, node: RadixNode, prefix: str, path_segments: list[str]) -> None:
        """Recursively merge nodes from another router's tree."""
        if node.handlers:
            seg_path = "/".join(path_segments)
            full_path = prefix + "/" + seg_path if seg_path else prefix or "/"
            for method, info in node.handlers.items():
                segments = self._split_path(full_path)
                cur, param_names = self._insert_path_into_tree(self._root, segments, full_path)

                route_info = self._build_merged_route_info(info, param_names, full_path)
                # Reuse the parent's pre-computed handler plan; route_dep_plans
                # are rebuilt from the combined dependencies since this router's
                # router_dependencies may have been prepended above.
                is_ws = method.upper() == ROUTE_METHOD_WEBSOCKET
                self._finalize_plans(route_info, is_ws=is_ws, reuse_handler_plan=info.handler_plan)

                # Propagate slash-handling flags from the source node so a
                # router declared with `strict_slashes=False` keeps that
                # behaviour after merge, and `add_route` calls that set
                # `trailing_slash` on the source see the flag reflected
                # on the merged node. Node flags are independent of the
                # handler-table commit below, so order does not matter.
                if node.trailing_slash:
                    cur.trailing_slash = True
                if node.unslashed_variant:
                    cur.unslashed_variant = True
                if node.tolerant_slash:
                    cur.tolerant_slash = True

                self._commit_merged_method(
                    cur.handlers, method, route_info, info.name, full_path, param_names
                )

        for child in node.static_children.values():
            self._merge_node(child, prefix, path_segments + [child.segment])
        for child in node.param_children:
            self._merge_node(child, prefix, path_segments + [child.segment])
        if node.wildcard_child is not None:
            self._merge_node(
                node.wildcard_child, prefix, path_segments + [node.wildcard_child.segment]
            )


def _readd_route(
    target: Router, full_path: str, methods: list[str], info: RouteInfo, endpoint: str
) -> None:
    """Re-register `info` onto `target` under `full_path`/`endpoint`.

    Single source of truth for the `RouteInfo`-to-`add_route` field mapping
    shared by blueprint splicing (`Veloce.register_blueprint`) and nested
    blueprint composition (`Blueprint.register_blueprint`). Keeping the kwarg
    block here means a new `RouteInfo` field is forwarded on every
    re-registration path, not silently dropped on one of them.
    """
    target.add_route(
        path=full_path,
        handler=info.handler,
        methods=methods,
        strict_slashes=info.strict_slashes,
        dependencies=info.dependencies,
        response_model=info.response_model,
        tags=info.tags,
        summary=info.summary,
        name=endpoint,
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
        subdomain=info.subdomain,
        host=info.host,
        expose_as_mcp_tool=info.expose_as_mcp_tool,
        mcp_description=info.mcp_description,
        expose_as_mcp_resource=info.expose_as_mcp_resource,
        mcp_resource_uri=info.mcp_resource_uri,
        mcp_resource_mime_type=info.mcp_resource_mime_type,
        mcp_resource_size=info.mcp_resource_size,
        mcp_resource_annotations=info.mcp_resource_annotations,
        mcp_meta=info.mcp_meta,
        stream=info.stream,
        mcp_scopes=list(info.mcp_scopes) if info.mcp_scopes else None,
        mcp_icons=info.mcp_icons,
        mcp_task_support=info.mcp_task_support,
        exclude_middleware=(
            [*info.excluded_middleware[0], *info.excluded_middleware[1]]
            if info.excluded_middleware
            else None
        ),
    )
