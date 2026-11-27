"""Aborter class + app.aborter attribute."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce, abort
from veloce.exceptions import Aborter, Forbidden, HTTPException, NotFound


def test_aborter_raises_typed_subclass_for_known_status():
    aborter = Aborter()
    with pytest.raises(NotFound):
        aborter(404)


def test_aborter_passes_detail_through():
    aborter = Aborter()
    with pytest.raises(Forbidden) as exc:
        aborter(403, "go away")
    assert exc.value.detail == "go away"


def test_aborter_default_detail_uses_http_phrase():
    aborter = Aborter()
    with pytest.raises(NotFound) as exc:
        aborter(404)
    assert exc.value.detail == "Not Found"


def test_aborter_extra_mapping_wins():
    """Caller-provided mapping overrides the default class lookup."""

    class MyCustom404(HTTPException):
        pass

    aborter = Aborter(extra_mapping={404: MyCustom404})
    with pytest.raises(MyCustom404):
        aborter(404)


def test_aborter_unknown_code_falls_back_to_base():
    aborter = Aborter()
    with pytest.raises(HTTPException) as exc:
        aborter(599, "weird")
    assert exc.value.status_code == 599


def test_app_aborter_lazy_singleton():
    app = Veloce(openapi_url=None)
    a = app.aborter
    b = app.aborter
    assert a is b  # cached on first access


def test_app_aborter_settable():
    app = Veloce(openapi_url=None)
    custom = Aborter()
    app.aborter = custom
    assert app.aborter is custom


def test_abort_raises():
    with pytest.raises(HTTPException) as exc_info:
        abort(404)
    assert exc_info.value.status_code == 404


def test_abort_with_detail():
    with pytest.raises(HTTPException) as exc_info:
        abort(403, "Forbidden")
    assert exc_info.value.detail == "Forbidden"


def test_abort_honours_the_bound_apps_custom_mapping():
    """`app.aborter(404)` is documented as equivalent to `abort(404)`.

    The defect: `abort()` re-implemented the lookup without the mapping, so an
    app that registered a custom class for a code got it from `app.aborter`
    and silently did not get it from the `abort()` every example teaches.
    """

    class MyNotFound(NotFound):
        pass

    app = Veloce(openapi_url=None)
    app.aborter = Aborter(extra_mapping={404: MyNotFound})
    with app.app_context(), pytest.raises(MyNotFound) as exc:
        abort(404)
    assert exc.value.detail == "Not Found"


def test_abort_outside_an_app_context_uses_the_default_lookup():
    with pytest.raises(NotFound) as exc:
        abort(404)
    assert type(exc.value) is NotFound


def test_abort_under_an_app_without_a_mapping_is_unchanged():
    app = Veloce(openapi_url=None)
    with app.app_context(), pytest.raises(NotFound) as exc:
        abort(404, headers={"X-Reason": "gone"})
    assert type(exc.value) is NotFound
    assert exc.value.headers == {"X-Reason": "gone"}


async def test_abort_in_handler():
    app = Veloce(openapi_url=None)

    @app.get("/fail")
    async def fail(request: Request):
        abort(418, "I'm a teapot")

    resp = await app.handle_request(make_request(path="/fail"))
    assert resp.status_code == 418
