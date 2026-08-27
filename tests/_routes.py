"""Select one registered route from a test.

Three tests reached for a `for` / `if` / `break` / `for`-`else` search over
`app._collect_all_routes()`, which is five lines to say "the route at this
path", reports `route not found` for the different fault of two routes sharing
one path, and reaches past the public seam to do it.

`Router.iter_routes()` is that seam: it returns the `RouteInfo` records rather
than `app.routes`' six-field summary, which is exactly what a test inspecting a
route needs.
"""

from __future__ import annotations

from veloce import Veloce
from veloce.routing.router import RouteInfo


def route_at(app: Veloce, path: str, *, include_hidden: bool = False) -> RouteInfo:
    """The single route registered at `path`.

    Fails distinctly for none and for more than one: a test that means to
    inspect *the* route at a path has a different problem in each case.
    """
    found = [
        info
        for _method, registered, info in app.iter_routes(include_hidden=include_hidden)
        if registered == path
    ]
    assert found, f"no route registered at {path!r}"
    assert len(found) == 1, f"{len(found)} routes registered at {path!r}"
    return found[0]
