"""Router — radix-tree routing with path parameters, method dispatch, and route groups."""

from __future__ import annotations

import functools
import keyword
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from typing_extensions import Doc

from veloce._constants import MSG_SUCCESSFUL_RESPONSE
from veloce._handler_plan import (
    _NO_PATH_PARAMS,
    K_REQUEST,
    build_plan,
    build_route_dep_plans,
)
from veloce._internal import T_route
from veloce._model_backend import resolve_response_contract
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

# Re-exported so `veloce.routing.router.X` keeps resolving.
from veloce.routing._node import RadixNode, _converter_sort_key
from veloce.routing._regex import RegexRoute
from veloce.routing.converters import (
    Converter,
    StringConverter,
    _iter_placeholders,
    build_route_regex,
    is_regex_path,
    parse_converter,
    path_param_converters,
)
from veloce.routing.route_info import (
    MCPRouteOptions as MCPRouteOptions,
)
from veloce.routing.route_info import (
    RouteHandler,
    RouteInfo,
    RouteMatch,
)
from veloce.status import HTTP_200_OK

_logger = logging.getLogger(__name__)

# Distinguishes "not supplied" - derive the model from the handler's return
# annotation - from an explicit `response_model=None`, which declares none.
_INFER_RESPONSE_MODEL: Any = object()

# ── Module constants and helpers ───────────────────────────


# `"error"` raises, `"warn"` logs and replaces, `"override"` is silent.
_DUPLICATE_POLICIES = frozenset({"error", "warn", "override"})

# Shared by `add_route` and `route` rather than copied, so a change reaches both
# signatures at once and neither tooltip can drift from the other.
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
    entries: Sequence[str | type[Any]] | None,
) -> tuple[frozenset[str], tuple[type[Any], ...]] | None:
    """Split `exclude_middleware` into its name set and its type tuple.

    `None` when the route excludes nothing, which the dispatch path tests for
    before doing any filtering. An entry that is neither a name nor a
    `Middleware` subclass is a `TypeError` here rather than an exclusion that
    silently matches nothing - which is the failure this shape exists to end.
    """
    if not entries:
        return None
    names: set[str] = set()
    types: list[type[Any]] = []
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


# `/items/{slug}` and `/items/{id}` both become `/items/{}`, so a tree route and
# a regex fallback mapping to one effective path can be detected.
_PARAM_SHAPE_RE = re.compile(r"\{[^{}]*\}")

#: The unreserved set (RFC 3986 Sec. 2.3) plus the two `pchar` extras. A value
#: made only of these is its own encoding, so `url_for` skips `quote` entirely.
_NEEDS_QUOTE_IN_SEGMENT = re.compile(r"[^A-Za-z0-9_.~:@-]")
#: The same, plus `/` for a greedy `path` converter.
_NEEDS_QUOTE_IN_PATH = re.compile(r"[^A-Za-z0-9_.~:@/-]")


def _path_shape(path: str) -> str:
    """Return `path` with every `{param}` collapsed to `{}` for shape compare."""
    return _PARAM_SHAPE_RE.sub("{}", path)


@functools.lru_cache(maxsize=512)
def _cached_split_path(path: str) -> tuple[str, ...]:
    """Split `path` into segments, keeping any the path actually contains.

    An absolute path always starts with an empty part, and a trailing slash
    adds one; both are structural and dropped. Every *other* empty is a real
    empty segment (`//admin/x`, `/a//b`) and is kept, so the radix walk looks
    for a child named `""`, finds none, and the path simply does not match.

    Dropping them instead made `//admin/x` produce the same segments as
    `/admin/x` and match the same route, while `request.path` still read the
    original - which is how a prefix check and the router came to disagree.
    Keeping them makes the refusal structural: there is nothing to check on the
    match path, because a path that cannot be walked cannot be matched.
    """
    parts = path.split("/")
    end = len(parts) - 1 if len(parts) > 1 and parts[-1] == "" else len(parts)
    start = 1 if parts and parts[0] == "" else 0
    return tuple(parts[start:end])


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
    return path_param_converters(template)


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
        # Catches accidental route shadowing at startup rather than letting a
        # wrong handler fire in production.
        if on_duplicate not in _DUPLICATE_POLICIES:
            raise ValueError(
                f"on_duplicate must be one of {sorted(_DUPLICATE_POLICIES)}, got {on_duplicate!r}"
            )
        self.on_duplicate = on_duplicate
        # Copied: appended to as routes register, so aliasing mutates the caller's.
        self.tags = list(tags or [])
        # Once set it is the class every return value goes to, so a text class
        # given a `dict` raises rather than falling back to `JSONResponse`.
        self.default_response_class = default_response_class
        # Per-route `dependencies=` is *appended* to this list, not replacing it,
        # so both fire and the route-specific ones run last.
        self.router_dependencies = list(dependencies or [])
        # Each route's `responses=` overlays on top, per-route status codes
        # winning on collision.
        self.router_responses: dict[int, dict[str, Any]] = dict(responses or {})
        self._root = RadixNode()
        # (method, exact-path) -> RouteInfo, so a literal path resolves in one
        # hash lookup; built lazily and dropped to None on any registration.
        self._static_routes: dict[tuple[str, str], RouteInfo] | None = None
        # Route name -> (path_template, param_names), for url_for reverse lookup.
        self._named_routes: dict[str, tuple[str, list[str]]] = {}
        # Route name -> {param_name: converter}, so `url_for` validates each value
        # through the converter the matcher applies and the URL is sure to match.
        self._reverse_converters: dict[str, dict[str, Converter]] = {}
        # Empty for the common case, and `match()` guards on it, so the radix
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
        # Any tree mutation invalidates the fast map; `match()` rebuilds it.
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

                # Reused only on the same name AND converter, whose constraint
                # text is in the key so distinct bounds get their own node.
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
                # Legacy `*` syntax; the slot is reused so both routes share it.
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

    # The idempotent-remount exemption requires ALL of these to match. Listed
    # rather than diffed, so a new route-defining slot forces a decision here.
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
            # Carried from the source, or a sub-router merged via
            # `include_router` loses its subdomain / host / defaults.
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
        # `stream` is assigned after construction rather than in `__init__`, so
        # it has to be carried across explicitly.
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
        # The shared empty set, rather than one allocation per registration.
        path_params = (
            frozenset(route_info.param_names) if route_info.param_names else _NO_PATH_PARAMS
        )
        if reuse_handler_plan is not None:
            route_info.handler_plan = reuse_handler_plan
        else:
            route_info.handler_plan = build_plan(
                route_info.handler, websocket=is_ws, path_params=path_params
            )
        route_info.route_dep_plans = build_route_dep_plans(
            route_info.dependencies, websocket=is_ws, path_params=path_params
        )
        # Derived from the handler, like the plan above, so every registration
        # path picks up the listener's message contract here.
        route_info.ws_messages = (
            getattr(route_info.handler, "_ws_message_contract", None) if is_ws else None
        )
        slots = route_info.handler_plan.slots
        has_deps = bool(route_info.route_dep_plans)
        route_info.is_trivial_plan = not slots and not has_deps
        # The fast path binds `{"request": request}` with no resolver.
        route_info.is_request_only_plan = (
            len(slots) == 1 and slots[0].kind == K_REQUEST and not has_deps
        )
        if route_info.is_request_only_plan:
            route_info.request_param_name = slots[0].name
        # An async handler with a trivial or request-only plan and none of the
        # features the fast path cannot honour; a WebSocket route never applies.
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
            Sequence[str | type[Any]] | None,
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
        # Before any node or regex is built, so both branches share one error.
        _check_duplicate_params(full_path)
        node, regex_route, param_names = self._classify_route_path(full_path, strict_slashes)

        route_name = name or handler.__name__
        # Router-level dependencies run first: outer scope before inner.
        combined_deps = list(self.router_dependencies)
        if dependencies:
            combined_deps.extend(dependencies)
        # A return annotation naming a model becomes the enforced contract; an
        # explicit `response_model=` wins, and an explicit `None` opts out.
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

        # A WebSocket route's plan is built in websocket mode, so the connection
        # binds by annotation or name through the shared resolver.
        is_ws = any(m.upper() == ROUTE_METHOD_WEBSOCKET for m in methods)
        self._finalize_plans(route_info, is_ws=is_ws)

        if regex_route is not None:
            handler_table = regex_route.handlers
        else:
            assert node is not None
            handler_table = node.handlers
        self._commit_route_methods(methods, handler_table, full_path, route_info, route_name)

        # A name taken by a *different* path is a collision `on_duplicate` never
        # saw: `url_for` would start resolving somewhere else.
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
        # `/foo` and `/foo/` share this node, so one form is recorded without
        # clearing the other's slash strictness.
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
                # Nothing is mutated yet, so a raise leaves the router unchanged.
                self._on_duplicate_route(full_path, mkey, existing, route_info)
                # Pass 2 drops the displaced route's reverse entry after the check.
                replaceable.append((mkey, existing))
        for mkey, existing in replaceable:
            # So `url_for(old_name)` stops resolving to a dead route.
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
                # A converter rejecting its matched group is a miss, not an error.
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
                # RFC 9110 Sec. 9.3.2: HEAD falls back to GET, and the body
                # strip is transport-level, so aliasing it here is equivalent.
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
        # Zero cost when no regex route is registered: the guard comes first.
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

        # A route registered with a slash matches only slashed requests, and
        # vice versa; `tolerant_slash` skips the gate, as does registering both.
        if _slash_mismatch(result, request_has_slash):
            return None

        # Conforming clients send uppercase, so try the raw form first.
        # `.isupper()`, since CPython promises no identity for `str.upper()`.
        handler_info = result.handlers.get(method)
        if handler_info is None:
            if method.isupper():
                method_upper = method
            else:
                method_upper = method.upper()
                handler_info = result.handlers.get(method_upper)
            # RFC 9110 Sec. 9.3.2: HEAD falls back to GET, body stripped later.
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
        # With no alternative branch to backtrack into, recursing per static
        # segment would burn one Python frame per hop for no gain.
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

        # Static first, an O(1) dict lookup; the recursion is still needed, since
        # this node may carry alternative param or wildcard branches.
        static_child = node.static_children.get(seg)
        if static_child is not None:
            result = self._match_node(static_child, segments, idx + 1, params)
            if result is not None:
                return result

        # Invariant: `params` is owned by the top-level `match()` and discarded
        # on None, so a key leaked by a failed branch stays in that dict.
        param_children = node.param_children
        single_param = len(param_children) == 1
        for child in param_children:
            converter = child.converter
            # Bound once, so the assignment and the rollback share one load.
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
        # The same predicate the match path uses, so `Allow` cannot advertise a
        # method that would not match.
        if node is not None and node.handlers and not _slash_mismatch(node, request_has_slash):
            methods.update(dict.fromkeys(node.handlers))
        if self._regex_routes:
            for route in self._regex_routes:
                m = self._regex_route_match(route, path)
                if m is None:
                    continue
                # A converter rejection is a full miss: a 404, never a 405.
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
            Sequence[str | type[Any]] | None,
            _DOC_EXCLUDE_MIDDLEWARE,
        ] = None,
        stream: Annotated[
            bool,
            _DOC_STREAM,
        ] = False,
    ) -> Callable[[T_route], T_route]:
        """Register a route for any set of HTTP methods.

        `exclude_middleware=["CSRFMiddleware"]` opts this route out of the
        named middleware (matched against each middleware's `name`), so a
        webhook or health-check route can skip CSRF, auth, or rate limiting
        without forking the middleware. Routes that declare no exclusions
        pay no extra per-request cost.
        """

        def decorator(func: T_route) -> T_route:
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

    def get(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`GET` route decorator. Safe and idempotent - RFC 9110 Sec. 9.3.1."""
        return self.route(path, methods=[HTTP_METHOD_GET], **kwargs)

    def post(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`POST` route decorator - RFC 9110 Sec. 9.3.3."""
        return self.route(path, methods=[HTTP_METHOD_POST], **kwargs)

    def put(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`PUT` route decorator. Idempotent - RFC 9110 Sec. 9.3.4."""
        return self.route(path, methods=[HTTP_METHOD_PUT], **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`PATCH` route decorator - RFC 5789."""
        return self.route(path, methods=[HTTP_METHOD_PATCH], **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`DELETE` route decorator. Idempotent - RFC 9110 Sec. 9.3.5."""
        return self.route(path, methods=[HTTP_METHOD_DELETE], **kwargs)

    def head(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`HEAD` route decorator. Like `GET` with no body - RFC 9110 Sec. 9.3.2."""
        return self.route(path, methods=[HTTP_METHOD_HEAD], **kwargs)

    def options(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`OPTIONS` route decorator - RFC 9110 Sec. 9.3.7."""
        return self.route(path, methods=[HTTP_METHOD_OPTIONS], **kwargs)

    def trace(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
        """`TRACE` route decorator - RFC 9110 Sec. 9.3.8."""
        return self.route(path, methods=[HTTP_METHOD_TRACE], **kwargs)

    def query(self, path: str, **kwargs: Any) -> Callable[[T_route], T_route]:
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
    ) -> Callable[[RouteHandler], RouteHandler]:
        """Register a WebSocket route via decorator."""

        def decorator(func: RouteHandler) -> RouteHandler:
            self.add_route(path=path, handler=func, methods=[ROUTE_METHOD_WEBSOCKET])
            return func

        return decorator

    websocket_route = websocket

    def websocket_listener(
        self,
        path: str,
        *,
        receive: str = "json",
        send: str = "json",
        on_connect: RouteHandler | Callable[..., Any] | None = None,
        on_disconnect: RouteHandler | Callable[..., Any] | None = None,
    ) -> Callable[[RouteHandler | Callable[..., Any]], RouteHandler | Callable[..., Any]]:
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
            handler, _contract = build_listener_handler(
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

        # Popped first, so what remains in `path_params` becomes query string.
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

        # So `url_for('item', id='abc')` on `/items/{id:int}` raises rather than
        # emitting a dead URL; a bare `{name}` has no converter and is skipped.
        converters = self._reverse_converters.get(name)
        if converters is None:
            converters = _reverse_converters_for(template)
            self._reverse_converters[name] = converters
        for pname, converter in converters.items():
            value = path_params[pname]
            text = str(value)
            # A segment-bounded converter's `match` never tests for `/`, and the
            # test belongs here, not on every matched parameter.
            ok, _ = converter.match(text)
            if ok and not converter.greedy and "/" in text:
                ok = False
            if not ok:
                raise ValueError(
                    f"Value {value!r} for path parameter {pname!r} is invalid for route {name!r}"
                )

        # Single-pass, so a value holding `{other_param}` corrupts nothing.
        out: list[str] = []
        pos = 0
        for ph in _iter_placeholders(template):
            out.append(template[pos : ph.start])
            if ph.name in path_params:
                # Unencoded, an opaque value could inject a query parameter or a
                # path segment; `:` and `@` are `pchar` (RFC 3986 Sec. 3.3).
                placeholder_converter = converters.get(ph.name)
                greedy = placeholder_converter is not None and placeholder_converter.greedy
                text = str(path_params[ph.name])
                # Every alphanumeric is unreserved, so the usual id or slug needs
                # neither. Measured 62 ns against 173 ns and 521 ns for `quote`.
                if not text.isalnum():
                    unsafe = _NEEDS_QUOTE_IN_PATH if greedy else _NEEDS_QUOTE_IN_SEGMENT
                    if unsafe.search(text):
                        text = quote(text, safe=":@/" if greedy else ":@")
                out.append(text)
            else:
                out.append(template[ph.start : ph.end])
            pos = ph.end
        out.append(template[pos:])
        path = "".join(out)

        # Anything left is a query parameter, in the caller's kwarg order.
        extras = {k: v for k, v in path_params.items() if k not in consumed}
        if extras:
            path = f"{path}?{urlencode(extras, doseq=True)}"

        if anchor is not None:
            path = f"{path}#{anchor}"

        if external or scheme or host:
            # SERVER_NAME is "host[:port]"; without it the absolute-URL request
            # was made outside a request context, so localhost is the fallback.
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

    # `url_for` is the canonical method; `url_path_for` is a thin alias.
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
        # `match()` consults the tree first, so a tree route owns its (method,
        # path-shape) pair and a same-shape regex route is not surfaced beside it.
        tree_owned: set[tuple[str, str]] = (
            set() if include_hidden else {(method, _path_shape(path)) for method, path, _ in routes}
        )
        # Not in the tree, so they are surfaced here in OpenAPI path form.
        for route in self._regex_routes:
            path = route.openapi_path
            for method, info in route.handlers.items():
                if include_hidden or (method != ROUTE_METHOD_WEBSOCKET and info.include_in_schema):
                    if not include_hidden and (method, _path_shape(path)) in tree_owned:
                        # A tree route owns this shape+method and wins at dispatch.
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
        extra_prefix = prefix.rstrip("/")
        self._merge_node(router._root, extra_prefix, [])
        # The child's regex fallback routes live outside the radix structure the
        # merge above walks, so they are merged explicitly under the prefix.
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
            # So a child route declared `strict_slashes=False` keeps it.
            if src.tolerant_slash:
                target.tolerant_slash = True

            for method, info in src.handlers.items():
                route_info = self._build_merged_route_info(info, param_names, full_path)
                # `route_dep_plans` are rebuilt, since this router's
                # `router_dependencies` may have been prepended above.
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
                # `route_dep_plans` are rebuilt, as in `_merge_regex_routes`.
                is_ws = method.upper() == ROUTE_METHOD_WEBSOCKET
                self._finalize_plans(route_info, is_ws=is_ws, reuse_handler_plan=info.handler_plan)

                # So a router declared `strict_slashes=False` keeps it after the
                # merge, and a source `trailing_slash` is reflected too.
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
