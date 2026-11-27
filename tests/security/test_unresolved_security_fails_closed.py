"""A guarded route refuses to register rather than serving unguarded.

`_salvage_hints` used to drop an unresolvable annotation, warn, and let the
parameter fall through to the query string - taking the `Security()` marker
with it. A route declaring `Security(current_user, ...)` answered `200` with
an attacker-supplied value while `current_user`, which raises 401
unconditionally, was never called.
"""

from __future__ import annotations

from typing import Annotated, Optional

import pytest

from veloce import Cookie, Depends, Header, Security, Veloce
from veloce import Security as Guard
from veloce.exceptions import HTTPException
from veloce.testclient import TestClient


def _denies(request):
    raise HTTPException(401, "unauthenticated")


def _allows(request):
    return "real-user"


def test_an_unresolvable_security_annotation_refuses_to_register():
    """NEGATIVE: the bypass is unreachable because the app does not start."""
    app = Veloce()

    with pytest.raises(TypeError, match="user"):

        @app.get("/me")
        async def me(request, user: Annotated[Missing, Security(_denies)]):  # noqa: F821
            return {"you_are": user}


def test_an_unresolvable_depends_annotation_refuses_to_register():
    """NEGATIVE: `Depends` carries behaviour too, so it fails closed the same way.

    The parameter is given a default deliberately: without one the no-default
    rule would refuse it anyway and this test would pass whether or not the
    marker is detected at all.
    """
    app = Veloce()

    with pytest.raises(TypeError, match="thing"):

        @app.get("/thing")
        async def thing(request, thing: Annotated[Missing, Depends(_allows)] = None):  # noqa: F821
            return {"thing": thing}


def test_a_live_annotated_security_marker_is_caught_too():
    """NEGATIVE: without PEP 563 the marker is an object, not text.

    `Annotated["Missing", Security(...)]` builds a real `_AnnotatedAlias`; its
    `Security` metadata reprs as `<veloce.dependency.Security object at 0x...>`,
    so a textual scan misses it while `__metadata__` finds it. Measured.

    The parameter has a default so only the metadata check can refuse it - the
    no-default rule would otherwise mask the very thing under test.
    """
    app = Veloce()
    annotation = Annotated["Missing", Security(_denies)]  # noqa: F821

    async def live(request, user=None):
        return {"you_are": user}

    live.__annotations__["user"] = annotation

    with pytest.raises(TypeError, match="user"):
        app.get("/live")(live)


def test_an_unresolvable_annotation_with_no_default_refuses_to_register():
    """NEGATIVE: no default means it degrades to a required query parameter."""
    app = Veloce()

    with pytest.raises(TypeError, match="item"):

        @app.get("/item")
        async def item(request, item: Missing):  # noqa: F821
            return {"item": item}


def test_an_unresolvable_annotation_with_a_default_still_only_warns():
    """POSITIVE: an optional parameter loses nothing, so it stays a warning.

    Failing closed here would refuse routes that are not vulnerable - the
    parameter has a value whether or not the client supplies one.
    """
    app = Veloce()

    with pytest.warns(UserWarning, match="could not resolve the annotation"):

        @app.get("/opt")
        async def opt(request, tag: Missing = "default"):  # noqa: F821
            return {"tag": tag}

    with TestClient(app) as client:
        assert client.get("/opt").json() == {"tag": "default"}


def test_a_resolvable_security_route_still_registers_and_still_guards():
    """POSITIVE: the fix must not break the declaration it protects."""
    app = Veloce()

    @app.get("/guarded")
    async def guarded(request, user: Annotated[str, Security(_denies)]):
        return {"you_are": user}

    with TestClient(app) as client:
        assert client.get("/guarded").status_code == 401


def test_a_resolvable_security_route_admits_an_authorised_caller():
    """POSITIVE: the guard runs and its value reaches the handler."""
    app = Veloce()

    @app.get("/ok")
    async def ok(request, user: Annotated[str, Security(_allows)]):
        return {"you_are": user}

    with TestClient(app) as client:
        assert client.get("/ok").json() == {"you_are": "real-user"}


def test_a_request_parameter_with_an_unresolvable_annotation_is_unaffected():
    """POSITIVE: `request` binds by name, so nothing was ever lost for it."""
    app = Veloce()

    @app.get("/byname")
    async def byname(request: Missing):  # noqa: F821
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/byname").json() == {"ok": True}


def test_a_security_marker_with_a_default_is_still_refused():
    """NEGATIVE: the marker alone is enough; it does not need the no-default rule.

    Isolates the textual marker check. A `Security()` parameter that has a
    default is not a required query parameter, so only detecting the marker can
    refuse it - and dropping the marker still means the dependency never runs.
    """
    app = Veloce()

    with pytest.raises(TypeError, match="user"):

        @app.get("/defaulted")
        async def defaulted(request, user: Annotated[Missing, Security(_denies)] = None):  # noqa: F821
            return {"you_are": user}


# ── the marker must be found by identity, not by the text the author typed ──


def test_an_aliased_security_import_is_still_refused():
    """NEGATIVE: `from veloce import Security as Guard` must not evade the rule.

    A textual scan for "Security(" matches the source the author typed, so an
    aliased import missed it. Combined with a default - which sidesteps the
    no-default rule - the guard never ran and the caller supplied the value:
    `GET /aliased?user=attacker` returned `200 {"user": "attacker"}`.
    """
    app = Veloce()

    with pytest.raises(TypeError, match="user"):

        @app.get("/aliased")
        async def aliased(user: Annotated[Missing, Guard(_denies)] = None):  # noqa: F821
            return {"user": user}


def test_a_header_marker_with_a_default_is_refused():
    """NEGATIVE: a credential declared header-borne must not become query-borne.

    `Header(alias="X-Api-Key")` was dropped with the annotation, so the value
    sent in the declared header was ignored and one supplied in the query
    string was accepted instead - and logged with the URL.
    """
    app = Veloce()

    with pytest.raises(TypeError, match="key"):

        @app.get("/secret")
        async def secret(key: Annotated[Missing, Header(alias="X-Api-Key")] = None):  # noqa: F821
            return {"key": key}


def test_a_cookie_marker_with_a_default_is_refused():
    """NEGATIVE: same for a cookie-borne session identifier."""
    app = Veloce()

    with pytest.raises(TypeError, match="sid"):

        @app.get("/cookie")
        async def cookie(sid: Annotated[Missing, Cookie(alias="session")] = None):  # noqa: F821
            return {"sid": sid}


def test_a_marker_built_by_a_factory_is_refused():
    """NEGATIVE: the marker need not be spelled inline to carry behaviour."""

    def make_guard():
        return Security(_denies)

    app = Veloce()

    with pytest.raises(TypeError, match="user"):

        @app.get("/factory")
        async def factory(user: Annotated[Missing, make_guard()] = None):  # noqa: F821
            return {"user": user}


def test_an_unmarked_optional_parameter_still_only_warns():
    """POSITIVE: widening to every marker must not start refusing safe routes.

    A plain optional parameter carries no marker and has a default, so nothing
    about where its value comes from changes when the annotation is dropped.
    """
    app = Veloce()

    with pytest.warns(UserWarning, match="could not resolve the annotation"):

        @app.get("/plain")
        async def plain(request, note: Missing = "default"):  # noqa: F821
            return {"note": note}

    with TestClient(app) as client:
        assert client.get("/plain").json() == {"note": "default"}


def test_an_optional_wrapped_security_marker_is_refused():
    """NEGATIVE: `Optional[Annotated[T, Security(...)]]` must not hide the marker.

    `Optional[X]` is a Union, so the `Annotated` is no longer outermost and a
    raw `__metadata__` read finds nothing. Python 3.10's `get_type_hints`
    produces exactly this shape for any parameter defaulting to `None`, so it
    is not an exotic spelling. Left unhandled the route registered and
    `GET /opt?user=attacker` returned `{"user": "attacker"}`.
    """
    app = Veloce()

    with pytest.raises(TypeError, match="user"):

        @app.get("/opt")
        async def opt(user: Optional[Annotated[Missing, Security(_denies)]] = None):  # noqa: F821, UP045
            return {"user": user}


# ── the subscript base itself may be the name that does not resolve ──


def test_an_unresolvable_annotated_base_still_refuses():
    """NEGATIVE: `Annotated` itself under TYPE_CHECKING must not hide the marker.

    ruff's flake8-type-checking (TC003) moves `from typing import Annotated`
    into the `TYPE_CHECKING` block, so the *subscript base* becomes the
    unresolvable name. The placeholder swallowed the whole expression and the
    `Security()` inside it was never seen: the route registered and
    `GET /me?user=attacker` returned `200 {"user": "attacker"}`.

    The parameter has a default, so only marker detection can refuse it.
    """
    app = Veloce()

    with pytest.raises(TypeError, match="user"):

        @app.get("/me")
        async def me(user: Ann[Missing, Security(_denies)] = None):  # noqa: F821
            return {"user": user}


def test_an_unresolvable_base_with_a_header_marker_refuses():
    """NEGATIVE: the same collapse, with a non-`Depends` marker."""
    app = Veloce()

    with pytest.raises(TypeError, match="key"):

        @app.get("/secret")
        async def secret(key: Ann[Missing, Header(alias="X-Api-Key")] = None):  # noqa: F821
            return {"key": key}


def test_an_unresolvable_base_carrying_no_marker_still_only_warns():
    """POSITIVE: recovering the metadata must not start refusing safe routes.

    A collapsed subscript with nothing load-bearing inside it loses nothing,
    so it stays a warning exactly as an unmarked optional parameter does.
    """
    app = Veloce()

    with pytest.warns(UserWarning, match="could not resolve the annotation"):

        @app.get("/plainsub")
        async def plainsub(note: Ann[Missing, int] = "default"):  # noqa: F821
            return {"note": note}

    with TestClient(app) as client:
        assert client.get("/plainsub").json() == {"note": "default"}
