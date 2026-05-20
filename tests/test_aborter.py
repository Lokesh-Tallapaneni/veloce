"""Aborter class + app.aborter attribute."""

from __future__ import annotations

import pytest

from veloce import Veloce
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
