"""Two different blueprints cannot share one name.

`register_blueprint`'s `already_registered` guard keyed on the blueprint's
*name*. It exists for a real case - mounting the same blueprint at a second
prefix, where appending its hooks twice would run each of them twice on one
request - but a second, different blueprint with the same name is not that case,
and was treated as one:

    /a/x hooks=['a'] | /b/y hooks=['a']

`/b/y` is blueprint `b`'s route, and it ran blueprint `a`'s `before_request`. So
it is not only that `b`'s hooks are skipped; `a`'s run in their place. An auth or
rate-limit guard registered on `b` is silently absent, and `a`'s runs against
routes it was never written for. Nothing warned.

There is no correct behaviour to fall back to. Both blueprints' routes take the
same `<name>.` endpoint prefix, so `url_for` is ambiguous, hook buckets collide,
and one of the two must lose. It is refused at registration instead, where the
mistake is.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Blueprint, Veloce


def _bp(name: str, prefix: str, ran: list[str]) -> Blueprint:
    bp = Blueprint(name, url_prefix=prefix)

    @bp.before_request
    async def guard(request):
        ran.append(name)

    @bp.get("/x")
    async def route():
        return {"ok": True}

    return bp


def test_two_blueprints_sharing_a_name_are_refused():
    """The regression, turned into a failure at the point of the mistake."""
    ran: list[str] = []
    app = Veloce(openapi_url=None)
    app.register_blueprint(_bp("shared", "/a", ran))

    with pytest.raises(ValueError, match="shared"):
        app.register_blueprint(_bp("shared", "/b", ran))


def test_the_refusal_says_what_to_do():
    """A message a reader can act on without opening the framework."""
    ran: list[str] = []
    app = Veloce(openapi_url=None)
    app.register_blueprint(_bp("shared", "/a", ran))

    with pytest.raises(ValueError, match="different name"):
        app.register_blueprint(_bp("shared", "/b", ran))


def test_distinct_names_still_register():
    """The control: the ordinary case must be untouched."""
    ran: list[str] = []
    app = Veloce(openapi_url=None)
    app.register_blueprint(_bp("a", "/a", ran))
    app.register_blueprint(_bp("b", "/b", ran))

    assert set(app.blueprints) == {"a", "b"}


async def test_each_blueprint_runs_only_its_own_hooks():
    """What the collision broke, asserted on the shape that now works."""
    ran: list[str] = []
    app = Veloce(openapi_url=None)
    app.register_blueprint(_bp("a", "/a", ran))
    app.register_blueprint(_bp("b", "/b", ran))

    ran.clear()
    await app.handle_request(make_request(path="/a/x"))
    assert ran == ["a"]

    ran.clear()
    await app.handle_request(make_request(path="/b/x"))
    assert ran == ["b"], "a route ran another blueprint's hooks"


def test_the_same_blueprint_may_still_be_mounted_twice():
    """The case the guard exists for, which the fix must not refuse."""
    ran: list[str] = []
    app = Veloce(openapi_url=None)
    bp = _bp("shared", "/a", ran)

    app.register_blueprint(bp)
    app.register_blueprint(bp, url_prefix="/b")

    assert app.url_for("shared.route") is not None


async def test_a_twice_mounted_blueprint_runs_its_hooks_once():
    """The reason the guard exists, stated so the fix cannot undo it."""
    ran: list[str] = []
    app = Veloce(openapi_url=None)
    bp = _bp("shared", "/a", ran)
    app.register_blueprint(bp)
    app.register_blueprint(bp, url_prefix="/b")

    ran.clear()
    await app.handle_request(make_request(path="/b/x"))

    assert ran == ["shared"], "the hook ran once per mount rather than once per request"


async def test_both_mounts_of_one_blueprint_serve():
    ran: list[str] = []
    app = Veloce(openapi_url=None)
    bp = _bp("shared", "/a", ran)
    app.register_blueprint(bp)
    app.register_blueprint(bp, url_prefix="/b")

    assert (await app.handle_request(make_request(path="/a/x"))).status_code == 200
    assert (await app.handle_request(make_request(path="/b/x"))).status_code == 200
