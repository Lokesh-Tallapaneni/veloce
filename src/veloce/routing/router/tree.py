"""Tree maintenance — insert paths and apply the duplicate-route policy."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from veloce.exceptions import DuplicateRouteError
from veloce.routing._node import RadixNode, _converter_sort_key
from veloce.routing.converters import (
    StringConverter,
    parse_converter,
)
from veloce.routing.route_info import (
    RouteInfo,
)
from veloce.routing.router.matching import MatchingMixin

_logger = logging.getLogger(__name__)


class TreeMixin(MatchingMixin):
    """Insert paths into the radix tree and apply the duplicate-route policy."""

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
        "mcp_annotations",
        "excluded_middleware",
    )

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
