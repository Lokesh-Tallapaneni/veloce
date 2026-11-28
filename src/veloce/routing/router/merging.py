"""Introspection and merge — list registered routes and splice another router's in."""

from __future__ import annotations

from typing import TYPE_CHECKING

from veloce._protocol_constants import (
    ROUTE_METHOD_WEBSOCKET,
)
from veloce.routing._node import RadixNode
from veloce.routing.route_info import (
    RouteInfo,
    RouteOptions,
)
from veloce.routing.router._helpers import (
    _path_shape,
)
from veloce.routing.router.urls import UrlsMixin

if TYPE_CHECKING:  # pragma: no cover
    from veloce.routing.router.core import Router


class MergingMixin(UrlsMixin):
    """Introspect registered routes and merge another router's into this one."""

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
            path_template=full_path,
            options=RouteOptions.from_info(info),
            excluded_middleware=info.excluded_middleware,
            dependencies=combined_deps if combined_deps else info.dependencies,
            response_model=info.response_model,
            tags=(info.tags or []) + list(self.tags),
            name=info.name,
            response_class=info.response_class or self.default_response_class,
            responses=(
                None
                if not self.router_responses and not info.responses
                else {**self.router_responses, **(info.responses or {})}
            ),
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
        """Include another router (a sub-router with its own prefix, tags, and hooks).

        Args:
            router: The router whose routes are merged in.
            prefix: Path prefix prepended to each included route.

        """
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
