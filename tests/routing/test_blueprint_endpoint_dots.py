"""A blueprint route's name may not contain a dot, because the dot is the separator.

An endpoint is `"{blueprint}.{route}"`, nested as `"{parent}.{child}.{route}"`,
and `_endpoint_blueprint` recovers the blueprint by splitting on the last dot.
That is correct for nesting - reading only the first segment made a child's
hooks apply to every route under the parent - and it is unrecoverable if the
route's own name also contains a dot.

`Blueprint("admin").get("/users", name="users.list")` produced the endpoint
`admin.users.list`, whose blueprint key reads as `admin.users` - a blueprint
that was never registered. Every blueprint-scoped registration was then skipped
for that one route: `before_request`, `after_request`, `teardown_request`,
`url_value_preprocessor`, `url_defaults`. A guard on the blueprint silently did
not run, per-route, with nothing reported.

The name is refused at registration instead, so the ambiguity cannot be
constructed.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Blueprint, Response, Veloce


def _guarded_blueprint(route_name: str | None) -> tuple[Veloce, list[str]]:
    """A blueprint whose `before_request` refuses, and one route on it."""
    ran: list[str] = []
    app = Veloce(openapi_url=None)
    bp = Blueprint("admin", url_prefix="/admin")

    @bp.before_request
    async def guard(request):
        ran.append("guard")
        return Response(body=b"denied", status_code=403)

    @bp.get("/users", name=route_name)
    async def users():
        return {"ok": True}

    app.register_blueprint(bp)
    return app, ran


async def test_an_undotted_route_runs_the_blueprints_guard():
    """The control: this always worked, and must keep working."""
    app, ran = _guarded_blueprint("users_list")

    response = await app.handle_request(make_request(path="/admin/users"))

    assert response.status_code == 403
    assert ran == ["guard"]


def test_a_dotted_route_name_is_refused_at_registration():
    """The regression, turned into a loud failure at the point of the mistake."""
    bp = Blueprint("admin", url_prefix="/admin")

    with pytest.raises(ValueError, match="dot"):

        @bp.get("/users", name="users.list")
        async def users():
            return {"ok": True}


def test_the_refusal_names_the_offending_name():
    """A message a reader can act on without opening the framework."""
    bp = Blueprint("admin")

    with pytest.raises(ValueError, match="users.list"):

        @bp.get("/users", name="users.list")
        async def users():
            return {"ok": True}


def test_an_app_level_route_may_still_contain_a_dot():
    """The rule is about the blueprint separator, so it applies to blueprints.

    An app-level endpoint has no blueprint prefix, so nothing is ambiguous and
    an existing app is not broken by this.
    """
    app = Veloce(openapi_url=None)

    @app.get("/x", name="users.list")
    async def x():
        return {"ok": True}

    assert app.url_for("users.list") == "/x"


async def test_a_nested_blueprints_hooks_still_scope_to_its_own_routes():
    """The property `rfind` was introduced for, which the fix must not undo."""
    ran: list[str] = []
    app = Veloce(openapi_url=None)
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/c")

    @child.before_request
    async def child_guard(request):
        ran.append("child")

    @parent.get("/own")
    async def own():
        return {"ok": True}

    @child.get("/own")
    async def child_own():
        return {"ok": True}

    parent.register_blueprint(child)
    app.register_blueprint(parent)

    ran.clear()
    await app.handle_request(make_request(path="/p/own"))
    assert ran == [], "the child's hook ran on a parent route"

    ran.clear()
    await app.handle_request(make_request(path="/p/c/own"))
    assert ran == ["child"], "the child's hook did not run on its own route"


def test_a_nested_blueprint_name_may_not_contain_a_dot_either():
    """The same ambiguity arrives through the blueprint's own name."""
    with pytest.raises(ValueError, match="dot"):
        Blueprint("admin.v2")
