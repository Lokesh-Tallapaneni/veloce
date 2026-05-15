"""Radix-tree router with path parameters, method dispatch, and route groups."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from veloce.routing.converters import (
    StringConverter,
    _Converter,
    parse_converter,
)

RouteHandler = Callable[..., Coroutine[Any, Any, Any]]


class RadixNode:
    """A node in the radix tree."""

    __slots__ = (
        "segment",
        "children",
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
        self.children: list[RadixNode] = []
        self.handlers: dict[str, RouteInfo] = {}  # method -> RouteInfo
        self.param_name: str | None = None
        self.is_param = False
        self.is_wildcard = False
        self.trailing_slash = False
        # When True, the slashed and unslashed forms both match without
        # redirect — set by `strict_slashes=False` on `add_route`.
        self.tolerant_slash = False
        # Converter applied at match time. `None` for static and wildcard nodes;
        # always set on param nodes (defaulting to StringConverter).
        self.converter: _Converter | None = None


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
        "subdomain",
        "host",
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
        response_description: str = "Successful Response",
        status_code: int = 200,
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
        # `openapi_extra` — an arbitrary dict deep-merged into
        # this route's OpenAPI operation object (lets users inject
        # vendor extensions, custom requestBody examples, etc.).
        self.openapi_extra = openapi_extra
        # the routing-rule `defaults` — fixed values merged into
        # `path_params` at dispatch (without overriding URL-matched
        # params), so two rules can share one handler with one rule
        # supplying a default for a segment the other carries in the URL.
        self.defaults = defaults or {}
        # OpenAPI `callbacks` — a dict of named Callback objects emitted
        # verbatim into the operation's `callbacks` field (OpenAPI 3.1
        # §4.8.8 — out-of-band requests the API issues back to a caller).
        self.callbacks = callbacks
        # Subdomain constraint — matched against the request's host at
        # dispatch time. `None` means "any host"; `"*"` means "any
        # subdomain of SERVER_NAME but not the apex".
        self.subdomain = subdomain
        # Host constraint — the full `Host` header must equal this value
        # exactly (case-insensitive). `None` means "any host". Broader
        # than `subdomain`, which only constrains the leftmost label.
        self.host = host
        # Pre-computed reflection plan — filled in by Router.add_route once
        # this RouteInfo has been constructed. Tests that build RouteInfo
        # directly will see `None` here and the resolver will fall back to
        # the build-plan-on-demand path.
        self.handler_plan: Any = None
        self.route_dep_plans: list[Any] = []


class RouteMatch:
    """Result of matching a path against the tree."""

    __slots__ = ("route_info", "path_params")

    def __init__(self, route_info: RouteInfo, path_params: dict[str, str]) -> None:
        self.route_info = route_info
        self.path_params = path_params


class Router:
    """High-performance radix-tree router with a decorator-based route API."""

    def __init__(
        self,
        prefix: str = "",
        tags: list[str] | None = None,
        default_response_class: Any = None,
        dependencies: list | None = None,
        responses: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        self.prefix = prefix.rstrip("/")
        self.tags = tags or []
        # a Response subclass used when a registered route
        # doesn't pick its own `response_class=`. Routes still override
        # per-call; this is just the fallback before the built-in default
        # (`JSONResponse` for dict/list returns) kicks in.
        self.default_response_class = default_response_class
        # Router-level dependencies — applied to every route
        # registered on this router. Per-route `dependencies=` is
        # *appended* to (not replaced by) the router-level list, so
        # both fire and the route-specific ones run last.
        self.router_dependencies = list(dependencies or [])
        # Router-level `responses=` dict. Each route's
        # `responses=` overlays on top — per-route status codes win on
        # collision; router-level supplies the rest (typically the
        # 404/403/422 shape every route shares).
        self.router_responses: dict[int, dict[str, Any]] = dict(responses or {})
        self._root = RadixNode()
        self._sub_routers: list[Router] = []
        self._middleware: list[Callable] = []
        self._named_routes: dict[
            str, tuple[str, list[str]]
        ] = {}  # name -> (path_template, param_names)

    def _split_path(self, path: str) -> list[str]:
        """Split path into segments."""
        return [s for s in path.strip("/").split("/") if s]

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
        response_description: str = "Successful Response",
        status_code: int = 200,
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
        has_trailing_slash = full_path.endswith("/") and full_path != "/"
        segments = self._split_path(full_path)
        param_names: list[str] = []

        node = self._root
        for seg in segments:
            if seg.startswith("{") and seg.endswith("}"):
                # Path parameter, with optional `:converter` suffix.
                spec = seg[1:-1]
                if ":" in spec:
                    param_name, _, conv_spec = spec.partition(":")
                else:
                    param_name, conv_spec = spec, ""
                converter = parse_converter(conv_spec) if conv_spec else StringConverter()
                param_names.append(param_name)

                # Find an existing param child with the same name AND matching
                # converter type; otherwise add a new one. Different converters
                # for the same name on the same segment slot would be ambiguous,
                # so we treat them as distinct param children.
                child = None
                for c in node.children:
                    if (
                        c.is_param
                        and c.param_name == param_name
                        and type(c.converter) is type(converter)  # noqa: E721
                    ):
                        child = c
                        break
                if child is None:
                    child = RadixNode(seg)
                    child.is_param = True
                    child.param_name = param_name
                    child.converter = converter
                    node.children.append(child)
                node = child
                # Greedy converter (path) must terminate the rule — it consumes
                # everything that follows.
                if converter.greedy:
                    break
            elif seg == "*":
                # Wildcard
                child = RadixNode(seg)
                child.is_wildcard = True
                node.children.append(child)
                node = child
                break
            else:
                # Static segment
                child = None
                for c in node.children:
                    if not c.is_param and not c.is_wildcard and c.segment == seg:
                        child = c
                        break
                if child is None:
                    child = RadixNode(seg)
                    node.children.append(child)
                node = child

        if has_trailing_slash:
            node.trailing_slash = True
        if strict_slashes is False:
            node.tolerant_slash = True

        route_name = name or handler.__name__
        # Merge router-level dependencies (registered at Router.__init__)
        # with the route-specific list. Router-level dependencies run
        # first (matches the documented semantics — outer scope before inner).
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
        )

        # Register named route for url_for
        self._named_routes[route_name] = (full_path, param_names)

        # Pre-compute the handler resolution plan once, here at registration.
        # Falls back to None if the handler isn't introspectable; the resolver
        # will rebuild on demand in that case.
        from veloce._handler_plan import build_plan, build_route_dep_plans

        route_info.handler_plan = build_plan(handler)
        route_info.route_dep_plans = build_route_dep_plans(route_info.dependencies)

        for method in methods:
            node.handlers[method.upper()] = route_info

    def match(self, method: str, path: str) -> RouteMatch | None:
        """Match a request path against the radix tree. O(k) where k = path depth."""
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

        method_upper = method.upper()
        handler_info = result.handlers.get(method_upper)
        # RFC 9110 §9.3.2: HEAD is semantically identical to GET except
        # the response carries no body. If no explicit HEAD is registered,
        # fall back to the GET handler — the dispatcher strips the body
        # on the way out.
        if handler_info is None and method_upper == "HEAD":
            handler_info = result.handlers.get("GET")
        if handler_info is None:
            return None

        return RouteMatch(route_info=handler_info, path_params=params)

    def _match_node(
        self, node: RadixNode, segments: list[str], idx: int, params: dict[str, Any]
    ) -> RadixNode | None:
        """Recursive radix tree traversal with per-converter validation."""
        if idx == len(segments):
            return node if node.handlers else None

        seg = segments[idx]

        # Try static match first (fastest path).
        for child in node.children:
            if not child.is_param and not child.is_wildcard and child.segment == seg:
                result = self._match_node(child, segments, idx + 1, params)
                if result is not None:
                    return result

        # Try param match — each candidate validates the segment via its
        # converter. Greedy converters (path) consume the remainder in one go.
        for child in node.children:
            if not child.is_param:
                continue
            converter = child.converter
            assert converter is not None  # always set in add_route
            if converter.greedy:
                rest = "/".join(segments[idx:])
                ok, coerced = converter.match(rest)
                if ok and child.handlers:
                    params[child.param_name] = coerced  # type: ignore[index]
                    return child
                # Greedy match either succeeds and terminates, or skip this child.
                continue
            ok, coerced = converter.match(seg)
            if not ok:
                continue
            params[child.param_name] = coerced  # type: ignore[index]
            result = self._match_node(child, segments, idx + 1, params)
            if result is not None:
                return result
            del params[child.param_name]  # type: ignore[arg-type]

        # Try wildcard (legacy `*` syntax — kept for back-compat).
        for child in node.children:
            if child.is_wildcard:
                params["_wildcard"] = "/".join(segments[idx:])
                return child

        return None

    def get_allowed_methods(self, path: str) -> list[str]:
        """Get allowed methods for a path (for 405 responses)."""
        segments = self._split_path(path)
        request_has_slash = path.endswith("/") and path != "/"
        params: dict[str, str] = {}
        node = self._match_node(self._root, segments, 0, params)
        if node is None:
            return []
        # Respect trailing slash matching (skipped when tolerant_slash is set).
        if not node.tolerant_slash and node.trailing_slash and not request_has_slash:
            return []
        if (
            not node.tolerant_slash
            and not node.trailing_slash
            and request_has_slash
            and node.handlers
        ):
            return []
        return list(node.handlers.keys())

    # ── Decorator API ───────────────────────

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
        response_description: str = "Successful Response",
        status_code: int = 200,
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
    ) -> Callable:
        """Generic route decorator."""

        def decorator(func: RouteHandler) -> RouteHandler:
            self.add_route(
                path=path,
                handler=func,
                methods=methods or ["GET"],
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
            )
            return func

        return decorator

    def get(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=["GET"], **kwargs)

    def post(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=["POST"], **kwargs)

    def put(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=["PUT"], **kwargs)

    def patch(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=["PATCH"], **kwargs)

    def delete(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=["DELETE"], **kwargs)

    def head(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=["HEAD"], **kwargs)

    def options(self, path: str, **kwargs) -> Callable:
        return self.route(path, methods=["OPTIONS"], **kwargs)

    def trace(self, path: str, **kwargs) -> Callable:
        """`TRACE` route decorator — RFC 9110 §9.3.8."""
        return self.route(path, methods=["TRACE"], **kwargs)

    def websocket(self, path: str) -> Callable:
        """WebSocket route decorator."""

        def decorator(func: RouteHandler) -> RouteHandler:
            self.add_route(path=path, handler=func, methods=["WEBSOCKET"])
            return func

        return decorator

    # `websocket_route` is an alias for the `websocket` decorator.
    websocket_route = websocket

    def add_websocket_route(self, path: str, handler: RouteHandler) -> None:
        """Imperative WebSocket route registration — ASGI shape.

        The non-decorator form of `@app.websocket(path)`.
        """
        self.add_route(path=path, handler=handler, methods=["WEBSOCKET"])

    def add_api_websocket_route(
        self, path: str, endpoint: RouteHandler, name: str | None = None
    ) -> None:
        """the imperative imperative WebSocket route registration.

        Mirrors `add_api_route` for WebSocket endpoints — the
        non-decorator form of `@app.websocket(path)`. `name` is
        accepted but currently unused.
        """
        self.add_route(path=path, handler=endpoint, methods=["WEBSOCKET"], name=name)

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
        route kwargs — `response_model`, `tags`, `dependencies`,
        `status_code`, `openapi_extra`, … — pass straight through.
        Defaults to `["GET"]` when `methods` is omitted.
        """
        self.add_route(
            path=path,
            handler=endpoint,
            methods=methods or ["GET"],
            **kwargs,
        )

    def url_for(self, name: str, **path_params: Any) -> str:
        """Reverse URL lookup by route name (`url_for`).

        Substitutes each `{name}` placeholder in the registered template
        with the matching `path_params` kwarg. Underscore-prefixed kwargs
        are control parameters (convention):

        - `_external=True` — return an absolute URL. Uses
          `app.config["SERVER_NAME"]` when set, otherwise falls back to
          `localhost`. Caller should override `_scheme`/`_host` for
          anything more specific.
        - `_scheme="https"` — override scheme on the absolute URL.
        - `_host="example.com"` — override host on the absolute URL.
        - `_anchor="section"` — append `#section`.
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
            # The stored template may include a converter suffix (e.g.
            # `{id:int}`); replace the whole `{name…}` segment.
            placeholder_start = path.find("{" + pname)
            if placeholder_start == -1:
                continue
            placeholder_end = path.find("}", placeholder_start)
            if placeholder_end == -1:
                continue
            path = path[:placeholder_start] + str(path_params[pname]) + path[placeholder_end + 1 :]

        # Anything left in path_params is a query-string parameter (the
        # behaviour). Order matches caller's kwarg order via dict insertion.
        extras = {k: v for k, v in path_params.items() if k not in consumed}
        if extras:
            from urllib.parse import urlencode

            path = f"{path}?{urlencode(extras, doseq=True)}"

        if anchor is not None:
            path = f"{path}#{anchor}"

        if external or scheme or host:
            # SERVER_NAME is "host[:port]"; without it, default to
            # localhost — the absolute-URL request was made outside a request
            # context where we'd otherwise know the host.
            cfg_host = None
            cfg_scheme = "http"
            if hasattr(self, "config"):
                cfg_host = self.config.get("SERVER_NAME")
                cfg_scheme = self.config.get("PREFERRED_URL_SCHEME", "http")
            netloc = host or cfg_host or "localhost"
            url_scheme = scheme or cfg_scheme
            return f"{url_scheme}://{netloc}{path}"

        return path

    # Veloce exposes this exact reverse-URL builder as `url_path_for`.
    # `url_for` is the canonical method; this is a thin
    # alias so calling code reads cleanly.
    url_path_for = url_for

    def _collect_all_routes(self) -> list[tuple[str, str, RouteInfo]]:
        """Collect all routes as (method, path, info) tuples — used for OpenAPI."""
        routes: list[tuple[str, str, RouteInfo]] = []
        self._walk_tree(self._root, [], routes)
        return routes

    def _walk_tree(
        self,
        node: RadixNode,
        path_parts: list[str],
        out: list[tuple[str, str, RouteInfo]],
    ) -> None:
        if node.handlers:
            path = "/" + "/".join(path_parts) if path_parts else "/"
            for method, info in node.handlers.items():
                if method != "WEBSOCKET" and info.include_in_schema:
                    out.append((method, path, info))
        for child in node.children:
            seg = child.segment
            if child.is_param:
                seg = "{" + (child.param_name or "") + "}"
            elif child.is_wildcard:
                seg = "{path}"
            self._walk_tree(child, path_parts + [seg], out)

    def include_router(self, router: Router, prefix: str = "") -> None:
        """Include another router (a sub-router with its own prefix, tags, and hooks)."""
        self._sub_routers.append(router)
        # Merge named routes
        self._named_routes.update(router._named_routes)
        # Collect all routes from sub-router and re-add with combined prefix
        extra_prefix = prefix.rstrip("/")
        self._merge_node(router._root, extra_prefix, [])

    def _merge_node(self, node: RadixNode, prefix: str, path_segments: list[str]) -> None:
        """Recursively merge nodes from another router's tree."""
        if node.handlers:
            seg_path = "/".join(path_segments)
            full_path = prefix + "/" + seg_path if seg_path else prefix or "/"
            for method, info in node.handlers.items():
                # Use a temporary Router with no prefix to avoid double-prefixing
                segments = self._split_path(full_path)
                param_names: list[str] = []
                cur = self._root
                for seg in segments:
                    if seg.startswith("{") and seg.endswith("}"):
                        spec = seg[1:-1]
                        if ":" in spec:
                            param_name, _, conv_spec = spec.partition(":")
                        else:
                            param_name, conv_spec = spec, ""
                        converter = parse_converter(conv_spec) if conv_spec else StringConverter()
                        param_names.append(param_name)
                        child = None
                        for c in cur.children:
                            if (
                                c.is_param
                                and c.param_name == param_name
                                and type(c.converter) is type(converter)  # noqa: E721
                            ):
                                child = c
                                break
                        if child is None:
                            child = RadixNode(seg)
                            child.is_param = True
                            child.param_name = param_name
                            child.converter = converter
                            cur.children.append(child)
                        cur = child
                        if converter.greedy:
                            break
                    elif seg == "*":
                        child = RadixNode(seg)
                        child.is_wildcard = True
                        cur.children.append(child)
                        cur = child
                        break
                    else:
                        child = None
                        for c in cur.children:
                            if not c.is_param and not c.is_wildcard and c.segment == seg:
                                child = c
                                break
                        if child is None:
                            child = RadixNode(seg)
                            cur.children.append(child)
                        cur = child

                route_info = RouteInfo(
                    handler=info.handler,
                    param_names=info.param_names,
                    dependencies=info.dependencies,
                    response_model=info.response_model,
                    tags=info.tags,
                    summary=info.summary,
                    name=info.name,
                    path_template=full_path,
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
                )
                # Reuse the parent's pre-computed plan — same handler, same plan.
                route_info.handler_plan = info.handler_plan
                route_info.route_dep_plans = info.route_dep_plans
                cur.handlers[method] = route_info

                # Update named routes
                self._named_routes[info.name] = (full_path, info.param_names)

        for child in node.children:
            self._merge_node(child, prefix, path_segments + [child.segment])
