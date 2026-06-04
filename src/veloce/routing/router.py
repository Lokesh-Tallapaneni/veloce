"""Router - radix-tree routing with path parameters, method dispatch, and route groups."""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Callable, Coroutine, Iterator, Sequence
from typing import Any
from urllib.parse import urlencode

from veloce._constants import MSG_SUCCESSFUL_RESPONSE
from veloce._protocol_constants import (
    HTTP_METHOD_DELETE,
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
    HTTP_METHOD_PATCH,
    HTTP_METHOD_POST,
    HTTP_METHOD_PUT,
    HTTP_METHOD_TRACE,
    ROUTE_METHOD_WEBSOCKET,
    URL_SCHEME_HTTP,
)
from veloce.routing.converters import (
    FloatConverter,
    IntConverter,
    PathConverter,
    StringConverter,
    UUIDConverter,
    _Converter,
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

# Valid `on_duplicate` policies for a router. `"error"` raises, `"warn"` logs
# and replaces, `"override"` replaces silently.
_DUPLICATE_POLICIES = frozenset({"error", "warn", "override"})

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


def _reverse_converters_for(template: str) -> dict[str, _Converter]:
    """Map each typed placeholder in `template` to its converter for url_for.

    A bare `{name}` (no spec) and a raw-regex placeholder (`{id:[0-9]+}`) have
    no single coercing converter, so they are omitted - those params accept any
    stringifiable value during reverse. Built-in, custom, and `any(...)` specs
    map to the same converter the radix matcher applies, so url_for can reject a
    value the matcher would never accept. `any(...)` is whitelisted explicitly
    because it carries parentheses that the bare-identifier test reads as regex.
    """
    converters: dict[str, _Converter] = {}
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
    """Reject a path that binds one parameter name twice.

    A duplicate is always a bug: on the radix path the second capture silently
    clobbers the first at match time; on the regex path `re.compile` raises an
    opaque "redefinition of group name" error. Catch both at registration with
    one clear, path-scoped error, using the same placeholder scanner both
    branches consume so the names checked are exactly the names bound.
    """
    seen: set[str] = set()
    for ph in _iter_placeholders(full_path):
        if ph.name in seen:
            raise ValueError(f"Route {full_path!r}: duplicate path parameter {ph.name!r}")
        seen.add(ph.name)


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
        self.param_name: str | None = None
        self.is_param = False
        self.is_wildcard = False
        self.trailing_slash = False
        # When True, the slashed and unslashed forms both match without
        # redirect - set by `strict_slashes=False` on `add_route`.
        self.tolerant_slash = False
        # Converter applied at match time. `None` for static and wildcard nodes;
        # always set on param nodes (defaulting to StringConverter).
        self.converter: _Converter | None = None


# Converter specificity - lower = more restrictive = tried first during
# match. Ensures `/items/{id:int}` beats `/items/{slug:str}` regardless
# of registration order. The sort runs once per `add_route` call (at app
# startup), never on the per-request match path.
_CONVERTER_PRIORITY: dict[type, int] = {
    UUIDConverter: 0,
    IntConverter: 1,
    FloatConverter: 2,
    StringConverter: 3,
    PathConverter: 4,
}


def _converter_sort_key(node: RadixNode) -> int:
    return _CONVERTER_PRIORITY.get(type(node.converter), 3)


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
        "subdomain",
        "host",
        "expose_as_mcp_tool",
        "mcp_description",
        "excluded_middleware",
        "_mw_chain_cache",
    )

    def __init__(
        self,
        handler: RouteHandler,
        param_names: list[str],
        dependencies: list | None = None,
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
        excluded_middleware: frozenset[str] | None = None,
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
        self.include_in_schema = include_in_schema
        self.responses = responses or {}
        # Explicit OpenAPI `operationId` override; falls back to the route
        # name during schema emission.
        self.operation_id = operation_id
        # `openapi_extra` - an arbitrary dict deep-merged into
        # this route's OpenAPI operation object (lets users inject
        # vendor extensions, custom requestBody examples, etc.).
        self.openapi_extra = openapi_extra
        # the routing-rule `defaults` - fixed values merged into
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
        # Set by `add_route` once the plans are built.
        self.is_trivial_plan = False
        self.is_request_only_plan = False
        # MCP exposure (contrib.mcp). `expose_as_mcp_tool` opts this route
        # into the MCP tool registry; `mcp_description` is the LLM-facing
        # description (separate from the docstring), required by the MCP
        # safety policy at registry-build time.
        self.expose_as_mcp_tool = expose_as_mcp_tool
        self.mcp_description = mcp_description
        # Named middleware this route opts out of. `None` (the common case)
        # means "run every registered middleware" - the dispatch hot path
        # then iterates the app's middleware list directly with zero extra
        # work. A non-`None` frozenset triggers the filtered-chain path,
        # whose result is memoised in `_mw_chain_cache` keyed on the app's
        # middleware-list version so the filter runs at most once per
        # (route, middleware-set) generation, not per request.
        self.excluded_middleware: frozenset[str] | None = excluded_middleware
        self._mw_chain_cache: tuple[int, list[Any], list[Any]] | None = None


class RouteMatch:
    """Result of matching a path against the tree."""

    __slots__ = ("route_info", "path_params")

    def __init__(self, route_info: RouteInfo, path_params: dict[str, Any]) -> None:
        self.route_info = route_info
        self.path_params = path_params


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
        # method -> RouteInfo, mirroring RadixNode.handlers so the regex
        # path returns the same shape as the tree path.
        self.handlers: dict[str, RouteInfo] = {}
        # Built-in converter per placeholder name, so matched groups are
        # coerced to the same Python types the radix tree produces
        # (`{n:int}` -> int, not "3"). Bare and raw-regex groups are absent.
        self.converters: dict[str, _Converter] = extract_regex_converters(template)
        # Mirrors `RadixNode.tolerant_slash` - set by `strict_slashes=False`
        # so a regex route accepts the missing/extra trailing slash too.
        self.tolerant_slash = False

    @property
    def openapi_path(self) -> str:
        """OpenAPI-style path string built from the template (`/users/{id}`)."""
        return _openapi_path_from_template(self.template)


class Router:
    """High-performance radix-tree router with a decorator-based route API."""

    def __init__(
        self,
        prefix: str = "",
        tags: list[str] | None = None,
        default_response_class: Any = None,
        dependencies: list | None = None,
        responses: dict[int, dict[str, Any]] | None = None,
        on_duplicate: str = "error",
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
        self.tags = tags or []
        # a Response subclass used when a registered route
        # doesn't pick its own `response_class=`. Routes still override
        # per-call; this is just the fallback before the built-in default
        # (`JSONResponse` for dict/list returns) kicks in.
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
        # Route name -> (path_template, param_names), for url_for reverse lookup.
        self._named_routes: dict[str, tuple[str, list[str]]] = {}
        # Route name -> {param_name: converter}, derived from the template on the
        # first url_for call and cached. Lets url_for validate each substituted
        # value through the same converter the matcher applies, so a reversed URL
        # is guaranteed to resolve. A param with no typed converter (bare
        # `{name}` or a raw-regex segment) is omitted and skips validation.
        self._reverse_converters: dict[str, dict[str, _Converter]] = {}
        # Regex fallback routes, in registration order. Empty for the common
        # case; `match()` guards on `if self._regex_routes:` so the radix
        # fast path pays nothing when no regex route is registered.
        self._regex_routes: list[RegexRoute] = []
        # template -> RegexRoute, so a second method on the same regex path
        # reuses one compiled pattern instead of appending a duplicate.
        self._regex_route_index: dict[str, RegexRoute] = {}

    # -- Route registration ---------------------------------------

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
        # Deferred import: veloce.exceptions pulls in the http response stack,
        # which transitively imports exceptions again; importing it at module
        # top would create a routing<->http import cycle. This path runs only
        # on an actual collision, never on the registration hot path.
        from veloce.exceptions import DuplicateRouteError

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

    def add_route(
        self,
        path: str,
        handler: RouteHandler,
        methods: list[str],
        dependencies: list | None = None,
        response_model: Any = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        name: str | None = None,
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
        strict_slashes: bool | None = None,
        subdomain: str | None = None,
        host: str | None = None,
        expose_as_mcp_tool: bool = False,
        mcp_description: str | None = None,
        exclude_middleware: Sequence[str] | None = None,
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
        has_trailing_slash = full_path.endswith("/") and full_path != "/"

        # Classify once, at registration. A path the radix tree cannot
        # express (partial-segment params, multi-brace segments, raw regex
        # converters, greedy `:path` with a suffix) goes onto the regex
        # fallback; everything else stays on the unchanged tree fast path.
        regex_route: RegexRoute | None = None
        if is_regex_path(full_path):
            regex_route = self._regex_route_index.get(full_path)
            if regex_route is None:
                pattern = build_route_regex(full_path)
                param_names = list(pattern.groupindex)
                regex_route = RegexRoute(full_path, pattern, param_names)
                self._regex_routes.append(regex_route)
                self._regex_route_index[full_path] = regex_route
            else:
                param_names = regex_route.param_names
            if strict_slashes is False:
                regex_route.tolerant_slash = True
            node = None
        else:
            segments = self._split_path(full_path)
            node, param_names = self._insert_path_into_tree(self._root, segments, full_path)

            if has_trailing_slash:
                node.trailing_slash = True
            if strict_slashes is False:
                node.tolerant_slash = True

        route_name = name or handler.__name__
        # Merge router-level dependencies (registered at Router.__init__)
        # with the route-specific list. Router-level dependencies run
        # first (matches the documented semantics - outer scope before inner).
        combined_deps = list(self.router_dependencies)
        if dependencies:
            combined_deps.extend(dependencies)
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
            excluded_middleware=frozenset(exclude_middleware) if exclude_middleware else None,
        )

        # Pre-compute the handler resolution plan once, here at registration.
        # Falls back to None if the handler isn't introspectable; the resolver
        # will rebuild on demand in that case.
        # Deferred import: _handler_plan depends on routing.params (same
        # subpackage), and pulling it at module level would create a
        # routing -> app-layer dependency direction violation. The import
        # runs once per route registration, not per request.
        from veloce._handler_plan import build_plan, build_route_dep_plans

        # A WebSocket route's plan is built in websocket mode so the
        # `WebSocket` connection is bound by annotation / name and its
        # dependency graph runs through the shared resolver.
        is_ws = any(m.upper() == ROUTE_METHOD_WEBSOCKET for m in methods)
        route_info.handler_plan = build_plan(handler, websocket=is_ws)
        route_info.route_dep_plans = build_route_dep_plans(route_info.dependencies, websocket=is_ws)
        # Classify the route for dispatch: a handler with no parameter
        # slots and no route-level dependencies needs nothing resolved.
        route_info.is_trivial_plan = (
            not route_info.handler_plan.slots and not route_info.route_dep_plans
        )
        # Request-only fast path: the handler takes only `request` and
        # the route has no dependencies. Skip DependencyResolver entirely
        # and bind kwargs = {"request": request} directly.
        from veloce._handler_plan import K_REQUEST  # same deferred-import rationale as above

        hp = route_info.handler_plan
        route_info.is_request_only_plan = (
            len(hp.slots) == 1 and hp.slots[0].kind == K_REQUEST and not route_info.route_dep_plans
        )

        # `node` is the radix leaf for tree routes; `regex_route` is set
        # instead for regex routes (the two branches above are mutually
        # exclusive, so exactly one of them holds the handler table).
        if regex_route is not None:
            handler_table = regex_route.handlers
        else:
            assert node is not None
            handler_table = node.handlers
        # Two-pass commit so a multi-method registration is atomic. The
        # `on_duplicate='error'` policy raises a DuplicateRouteError; if we
        # committed each method as we went, a collision on a *later* verb would
        # leave the *earlier* verbs already mutated into the handler table,
        # diverging from the caller's view (which catches the error expecting an
        # unchanged router). Pass 1 evaluates the policy for every method and
        # raises before any mutation; only once all methods pass do we mutate.
        replaceable: list[tuple[str, RouteInfo]] = []
        for method in methods:
            mkey = method.upper()
            existing = handler_table.get(mkey)
            if existing is not None and not self._allow_duplicate(existing, route_info):
                # May raise DuplicateRouteError on the 'error' policy; nothing
                # has been mutated yet, so the router is left fully unchanged.
                self._on_duplicate_route(full_path, mkey, existing, route_info)
                # warn/override allowed the replace - remember the displaced
                # route so pass 2 can drop its reverse entry after the check.
                replaceable.append((mkey, existing))
        # Pass 2: every method passed the policy, so commit them all. The
        # named-route reverse entry below is written only after this point, so a
        # caught DuplicateRouteError cannot leave url_for() polluted.
        for mkey, existing in replaceable:
            # The policy allowed the replace (warn/override). Drop the displaced
            # route's reverse entry when it had a different name, so
            # url_for(old_name) stops resolving to a dead route.
            self._drop_replaced_route_name(existing, route_name, handler_table, mkey)
        for method in methods:
            handler_table[method.upper()] = route_info

        # Register the named route for url_for only once the route is committed
        # to the handler table above. The reverse entry reflects the route that
        # actually wins on the override/warn replace paths, and is never written
        # if the duplicate policy raised. Drop any stale reverse-converter cache
        # so a re-registered name re-derives from its new template.
        self._named_routes[route_name] = (full_path, param_names)
        self._reverse_converters.pop(route_name, None)

    # -- Matching -------------------------------------------------

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

    def match(self, method: str, path: str) -> RouteMatch | None:
        """Match a request path. Radix tree first, regex fallback on a miss.

        O(k) where k = path depth on the tree fast path. The regex fallback
        runs only when the tree misses **and** regex routes are registered;
        the tree always wins over regex when both could match.
        """
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

        # Trailing slash strictness: route registered with slash only matches slashed requests.
        # `tolerant_slash` (per-route `strict_slashes=False`) skips this gate.
        if not result.tolerant_slash:
            if result.trailing_slash and not request_has_slash:
                return None
            if not result.trailing_slash and request_has_slash and result.handlers:
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
        segments: tuple[str, ...] | list[str],
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
            if converter is None:
                # add_route always populates this slot; a bare param child
                # with no converter is a routing-tree corruption, not a
                # client error. Loud failure beats a `'NoneType' is not
                # callable` two frames deeper.
                raise RuntimeError(f"radix-tree param child {child.param_name!r} has no converter")
            if converter.greedy:
                rest = "/".join(segments[idx:])
                ok, coerced = converter.match(rest)
                if ok and child.handlers:
                    params[child.param_name] = coerced  # type: ignore[index]
                    return child
                continue
            ok, coerced = converter.match(seg)
            if not ok:
                continue
            params[child.param_name] = coerced  # type: ignore[index]
            result = self._match_node(child, segments, idx + 1, params)
            if result is not None:
                return result
            if not single_param:
                del params[child.param_name]  # type: ignore[arg-type]

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
        if node is not None:
            # Respect trailing slash matching (skipped when tolerant_slash is set).
            slash_miss = (
                not node.tolerant_slash and node.trailing_slash and not request_has_slash
            ) or (
                not node.tolerant_slash
                and not node.trailing_slash
                and request_has_slash
                and node.handlers
            )
            if not slash_miss and node.handlers:
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

    # -- Decorator API --------------------------------------------

    def route(
        self,
        path: str,
        methods: list[str] | None = None,
        dependencies: list | None = None,
        response_model: Any = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        name: str | None = None,
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
        strict_slashes: bool | None = None,
        subdomain: str | None = None,
        host: str | None = None,
        expose_as_mcp_tool: bool = False,
        mcp_description: str | None = None,
        exclude_middleware: Sequence[str] | None = None,
    ) -> Callable:
        """Generic route decorator.

        `exclude_middleware=["CSRFMiddleware"]` opts this route out of the
        named middleware (matched against each middleware's `name`), so a
        webhook or health-check route can skip CSRF, auth, or rate limiting
        without forking the middleware. Routes that declare no exclusions
        pay no extra per-request cost.
        """

        def decorator(func: RouteHandler) -> RouteHandler:
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
                exclude_middleware=exclude_middleware,
            )
            return func

        return decorator

    def get(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=[HTTP_METHOD_GET], **kwargs)

    def post(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=[HTTP_METHOD_POST], **kwargs)

    def put(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=[HTTP_METHOD_PUT], **kwargs)

    def patch(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=[HTTP_METHOD_PATCH], **kwargs)

    def delete(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=[HTTP_METHOD_DELETE], **kwargs)

    def head(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=[HTTP_METHOD_HEAD], **kwargs)

    def options(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=[HTTP_METHOD_OPTIONS], **kwargs)

    def trace(self, path: str, **kwargs) -> Callable:
        """`TRACE` route decorator - RFC 9110 Sec. 9.3.8."""
        return self.route(path, methods=[HTTP_METHOD_TRACE], **kwargs)

    def websocket(self, path: str) -> Callable:
        """WebSocket route decorator."""

        def decorator(func: RouteHandler) -> RouteHandler:
            self.add_route(path=path, handler=func, methods=[ROUTE_METHOD_WEBSOCKET])
            return func

        return decorator

    # `websocket_route` is an alias for the `websocket` decorator.
    websocket_route = websocket

    def add_websocket_route(self, path: str, handler: RouteHandler) -> None:
        """Imperative WebSocket route registration - ASGI shape.

        The non-decorator form of `@app.websocket(path)`.
        """
        self.add_route(path=path, handler=handler, methods=[ROUTE_METHOD_WEBSOCKET])

    def add_api_websocket_route(
        self, path: str, endpoint: RouteHandler, name: str | None = None
    ) -> None:
        """the imperative imperative WebSocket route registration.

        Mirrors `add_api_route` for WebSocket endpoints - the
        non-decorator form of `@app.websocket(path)`. `name` is
        accepted but currently unused.
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
        """Imperative route registration.

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

    # -- Reverse URL lookup ---------------------------------------

    def url_for(self, name: str, **path_params: Any) -> str:
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
        path = template
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
            ok, _ = converter.match(str(path_params[pname]))
            if not ok:
                raise ValueError(
                    f"Value {path_params[pname]!r} for path parameter {pname!r} "
                    f"is invalid for route {name!r}"
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
            cfg_host = None
            cfg_scheme = URL_SCHEME_HTTP
            if hasattr(self, "config"):
                cfg_host = self.config.get("SERVER_NAME")
                cfg_scheme = self.config.get("PREFERRED_URL_SCHEME", URL_SCHEME_HTTP)
            netloc = host or cfg_host or "localhost"
            url_scheme = scheme or cfg_scheme
            return f"{url_scheme}://{netloc}{path}"

        return path

    # Veloce exposes this exact reverse-URL builder as `url_path_for`.
    # `url_for` is the canonical method; this is a thin
    # alias so calling code reads cleanly.
    url_path_for = url_for

    # -- Introspection and merge ----------------------------------

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
            seg = "{" + (child.param_name or "") + "}"
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
        from veloce._handler_plan import K_REQUEST, build_route_dep_plans

        for src in router._regex_routes:
            full_path = prefix + src.template if prefix else src.template
            target = self._regex_route_index.get(full_path)
            if target is None:
                pattern = build_route_regex(full_path)
                param_names = list(pattern.groupindex)
                target = RegexRoute(full_path, pattern, param_names)
                self._regex_routes.append(target)
                self._regex_route_index[full_path] = target
            else:
                param_names = target.param_names
            # Carry slash-tolerance from the source so a child route declared
            # with `strict_slashes=False` keeps it after merge.
            if src.tolerant_slash:
                target.tolerant_slash = True

            for method, info in src.handlers.items():
                combined_deps = list(self.router_dependencies)
                if info.dependencies:
                    combined_deps.extend(info.dependencies)
                route_info = RouteInfo(
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
                    openapi_extra=info.openapi_extra,
                    defaults=info.defaults,
                    callbacks=info.callbacks,
                    subdomain=info.subdomain,
                    host=info.host,
                    expose_as_mcp_tool=info.expose_as_mcp_tool,
                    mcp_description=info.mcp_description,
                    excluded_middleware=info.excluded_middleware,
                )
                route_info.handler_plan = info.handler_plan
                is_ws = method.upper() == ROUTE_METHOD_WEBSOCKET
                route_info.route_dep_plans = build_route_dep_plans(
                    route_info.dependencies, websocket=is_ws
                )
                route_info.is_trivial_plan = (
                    not route_info.handler_plan.slots and not route_info.route_dep_plans
                )
                hp = route_info.handler_plan
                route_info.is_request_only_plan = (
                    len(hp.slots) == 1
                    and hp.slots[0].kind == K_REQUEST
                    and not route_info.route_dep_plans
                )
                existing = target.handlers.get(method)
                if existing is not None and not self._allow_duplicate(existing, route_info):
                    self._on_duplicate_route(full_path, method, existing, route_info)
                    self._drop_replaced_route_name(existing, info.name, target.handlers, method)
                target.handlers[method] = route_info
                self._named_routes[info.name] = (full_path, param_names)
                self._reverse_converters.pop(info.name, None)

    def _merge_node(self, node: RadixNode, prefix: str, path_segments: list[str]) -> None:
        """Recursively merge nodes from another router's tree."""
        if node.handlers:
            seg_path = "/".join(path_segments)
            full_path = prefix + "/" + seg_path if seg_path else prefix or "/"
            for method, info in node.handlers.items():
                segments = self._split_path(full_path)
                cur, param_names = self._insert_path_into_tree(self._root, segments, full_path)

                combined_deps = list(self.router_dependencies)
                if info.dependencies:
                    combined_deps.extend(info.dependencies)

                route_info = RouteInfo(
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
                    # Carry constraints from the source RouteInfo - without
                    # these, sub-routers merged via include_router would
                    # silently lose their subdomain / host / openapi_extra
                    # / defaults / callbacks declarations.
                    openapi_extra=info.openapi_extra,
                    defaults=info.defaults,
                    callbacks=info.callbacks,
                    subdomain=info.subdomain,
                    host=info.host,
                    expose_as_mcp_tool=info.expose_as_mcp_tool,
                    mcp_description=info.mcp_description,
                    excluded_middleware=info.excluded_middleware,
                )
                # Reuse the parent's pre-computed handler plan.
                route_info.handler_plan = info.handler_plan

                # Rebuild route_dep_plans from the combined dependencies -
                # the parent's plans are stale when router_dependencies
                # were prepended above.
                from veloce._handler_plan import K_REQUEST, build_route_dep_plans

                is_ws = method.upper() == ROUTE_METHOD_WEBSOCKET
                route_info.route_dep_plans = build_route_dep_plans(
                    route_info.dependencies, websocket=is_ws
                )
                route_info.is_trivial_plan = (
                    not route_info.handler_plan.slots and not route_info.route_dep_plans
                )
                hp = route_info.handler_plan
                route_info.is_request_only_plan = (
                    len(hp.slots) == 1
                    and hp.slots[0].kind == K_REQUEST
                    and not route_info.route_dep_plans
                )
                existing = cur.handlers.get(method)
                if existing is not None and not self._allow_duplicate(existing, route_info):
                    self._on_duplicate_route(full_path, method, existing, route_info)
                    self._drop_replaced_route_name(existing, info.name, cur.handlers, method)
                cur.handlers[method] = route_info

                # Propagate slash-handling flags from the source node so a
                # router declared with `strict_slashes=False` keeps that
                # behaviour after merge, and `add_route` calls that set
                # `trailing_slash` on the source see the flag reflected
                # on the merged node.
                if node.trailing_slash:
                    cur.trailing_slash = True
                if node.tolerant_slash:
                    cur.tolerant_slash = True

                # Update named routes
                self._named_routes[info.name] = (full_path, param_names)
                self._reverse_converters.pop(info.name, None)

        for child in node.static_children.values():
            self._merge_node(child, prefix, path_segments + [child.segment])
        for child in node.param_children:
            self._merge_node(child, prefix, path_segments + [child.segment])
        if node.wildcard_child is not None:
            self._merge_node(
                node.wildcard_child, prefix, path_segments + [node.wildcard_child.segment]
            )
