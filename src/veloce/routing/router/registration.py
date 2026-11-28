"""Route registration — classify a path, build its record, commit its methods."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Annotated, Any

from typing_extensions import Doc

from veloce._constants import MSG_SUCCESSFUL_RESPONSE
from veloce._handler_plan import (
    _NO_PATH_PARAMS,
    K_REQUEST,
    build_plan,
    build_route_dep_plans,
)
from veloce._model_backend import resolve_response_contract
from veloce._protocol_constants import (
    ROUTE_METHOD_WEBSOCKET,
)
from veloce.routing._node import RadixNode
from veloce.routing._regex import RegexRoute
from veloce.routing.converters import (
    build_route_regex,
    is_regex_path,
)
from veloce.routing.route_info import (
    _INFER_RESPONSE_MODEL,
    RouteHandler,
    RouteInfo,
    RouteOptions,
)
from veloce.routing.router._docs import (
    _DOC_EXCLUDE_MIDDLEWARE,
    _DOC_MCP_ANNOTATIONS,
    _DOC_MCP_META,
    _DOC_MCP_RESOURCE_MIME_TYPE,
    _DOC_MCP_RESOURCE_URI,
    _DOC_MCP_TASK_SUPPORT,
    _DOC_RESPONSE_MODEL,
    _DOC_STREAM,
)
from veloce.routing.router._helpers import (
    _check_duplicate_params,
    _split_exclusions,
)
from veloce.routing.router.tree import TreeMixin
from veloce.status import HTTP_200_OK

_logger = logging.getLogger(__name__)


class RegistrationMixin(TreeMixin):
    """Register a route: classify its path, build its record, commit its methods."""

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
        # A POSITIONAL_ONLY parameter disqualifies it too - the fast path calls
        # the handler by keyword, which such a parameter cannot accept.
        route_info.is_fast_eligible = (
            not is_ws
            and route_info.handler_plan.is_coro
            and not route_info.handler_plan.positional_only
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
        *,
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
        mcp_annotations: Annotated[
            dict[str, Any] | None,
            _DOC_MCP_ANNOTATIONS,
        ] = None,
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
        self._add_route_options(
            path,
            handler,
            methods,
            RouteOptions(
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
                mcp_annotations=mcp_annotations,
                exclude_middleware=exclude_middleware,
                stream=stream,
            ),
        )

    def _add_route_from_info(
        self, full_path: str, methods: list[str], info: RouteInfo, endpoint: str
    ) -> None:
        """Register another router's route here under `full_path` and `endpoint`."""
        self._add_route_options(
            full_path, info.handler, methods, RouteOptions.from_info(info, name=endpoint)
        )

    def _add_route_options(
        self, path: str, handler: RouteHandler, methods: list[str], options: RouteOptions
    ) -> None:
        """Register a route from its collected options.

        The single choke point every registration path reaches - `add_route`,
        the `route` decorator, blueprint splicing and router merging - so the
        application brackets this, not each caller.
        """
        full_path = self.prefix + path
        # Before any node or regex is built, so both branches share one error.
        _check_duplicate_params(full_path)
        node, regex_route, param_names = self._classify_route_path(
            full_path, options.strict_slashes
        )

        route_name = options.name or handler.__name__
        # Router-level dependencies run first: outer scope before inner.
        combined_deps = list(self.router_dependencies)
        if options.dependencies:
            combined_deps.extend(options.dependencies)
        # A return annotation naming a model becomes the enforced contract; an
        # explicit `response_model=` wins, and an explicit `None` opts out.
        response_model = options.response_model
        if response_model is _INFER_RESPONSE_MODEL:
            response_model = resolve_response_contract(handler)

        merged_options = replace(
            options,
            dependencies=combined_deps if combined_deps else None,
            response_model=response_model,
            tags=(options.tags or []) + self.tags,
            name=route_name,
            response_class=options.response_class or self.default_response_class,
            responses=(
                None
                if not self.router_responses and not options.responses
                else {**self.router_responses, **(options.responses or {})}
            ),
        )
        route_info = RouteInfo(
            handler=handler,
            param_names=param_names,
            path_template=full_path,
            options=merged_options,
            excluded_middleware=_split_exclusions(options.exclude_middleware),
        )

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
