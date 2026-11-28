"""Route matching — resolve a method and path to a registered route."""

from __future__ import annotations

import re
from typing import Any

from veloce._protocol_constants import (
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
)
from veloce.routing._node import RadixNode
from veloce.routing._regex import RegexRoute
from veloce.routing.route_info import (
    RouteInfo,
    RouteMatch,
)
from veloce.routing.router._helpers import (
    _cached_split_path,
    _slash_mismatch,
)
from veloce.routing.router._state import RouterState


class MatchingMixin(RouterState):
    """Resolve a method and path to a registered route."""

    def _split_path(self, path: str) -> tuple[str, ...]:
        """Split path into segments (cached)."""
        return _cached_split_path(path)

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

        Args:
            method: HTTP method of the request.
            path: Request path.

        Returns:
            The matched route with its path parameters, or `None`.

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
