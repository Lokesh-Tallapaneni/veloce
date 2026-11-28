"""Router state — the constructor and the tables every layer reads."""

from __future__ import annotations

from typing import Annotated, Any

from typing_extensions import Doc

from veloce.routing._node import RadixNode
from veloce.routing._regex import RegexRoute
from veloce.routing.converters import (
    Converter,
)
from veloce.routing.route_info import (
    RouteInfo,
)
from veloce.routing.router._helpers import (
    _DUPLICATE_POLICIES,
)


class RouterState:
    """Per-router state - the constructor and the tables every layer reads."""

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
        self.on_duplicate = str(on_duplicate)
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
