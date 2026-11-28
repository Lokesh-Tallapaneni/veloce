"""Router package — gateway preserving the `veloce.routing.router` import paths.

`Router` is `core.Router` over a chain of focused layers, each a mixin on the
one before it: `_state` (constructor and tables), `matching`, `tree`,
`registration`, `decorators`, `urls` and `merging`. The record types it works
with live in `veloce.routing.route_info` and are re-exported here by name, so
every `from veloce.routing.router import X` path keeps resolving unchanged.
"""

from __future__ import annotations

from veloce.routing._node import RadixNode
from veloce.routing._regex import RegexRoute
from veloce.routing.route_info import (
    MCPRouteOptions,
    RouteHandler,
    RouteInfo,
    RouteMatch,
    RouteOptions,
)
from veloce.routing.router.core import Router

__all__ = [
    "MCPRouteOptions",
    "RadixNode",
    "RegexRoute",
    "RouteHandler",
    "RouteInfo",
    "RouteMatch",
    "RouteOptions",
    "Router",
]
