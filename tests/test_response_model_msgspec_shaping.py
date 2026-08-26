"""`response_model` filters a msgspec response as it filters a Pydantic one.

`_apply_response_model` was written against Pydantic's concrete API, and
`_dispatch_request` skipped it entirely for a msgspec-struct model or a
`list[Struct]`. So `response_model` - the documented way to say "these are the
fields that leave the process" - silently did nothing on one of the advertised
backends:

    response_model=Public,  returns Private   ->  {"id":1,"secret":"TOPSECRET"}
    response_model=PPublic, returns PPrivate  ->  {"id":1}

Same route shape, same declared contract, two backends, and only one of them
honoured it. The subclass-leak defence deliberately added to the Pydantic branch
had no equivalent for the other two.

`shape_through_model` in `_model_backend` was supposed to be the one shaper for
every backend - its docstring says so - but it reached `adapter_for`, which is
Pydantic's `TypeAdapter`, and that raises `PydanticSchemaGenerationError` on a
msgspec Struct. It grew a msgspec branch built on `msgspec.convert`, which
filters undeclared fields and still rejects a payload that does not conform.

These tests are written per backend and asserted *against each other*, so a
future change that fixes one and not the other fails here.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from veloce import Veloce
from veloce.testclient import TestClient

msgspec = pytest.importorskip("msgspec", reason="the msgspec backend is not installed")


class Public(msgspec.Struct):
    id: int


class Private(Public):
    secret: str


class PPublic(BaseModel):
    id: int


class PPrivate(PPublic):
    secret: str


def _serve(model, value, **route_kwargs):
    app = Veloce(openapi_url=None)

    @app.get("/r", response_model=model, **route_kwargs)
    async def route():
        return value

    return TestClient(app).get("/r")


# ── the leak, both backends ──────────────────────────────────────────


def test_a_msgspec_subclass_does_not_leak_its_extra_fields():
    """The defect: `secret` reached the wire."""
    assert _serve(Public, Private(id=1, secret="TOPSECRET")).json() == {"id": 1}


def test_a_pydantic_subclass_does_not_leak_its_extra_fields():
    assert _serve(PPublic, PPrivate(id=1, secret="TOPSECRET")).json() == {"id": 1}


def test_both_backends_shape_identically():
    """The property that was broken: one declared contract, one behaviour."""
    msgspec_body = _serve(Public, Private(id=1, secret="TOPSECRET")).json()
    pydantic_body = _serve(PPublic, PPrivate(id=1, secret="TOPSECRET")).json()
    assert msgspec_body == pydantic_body


def test_a_msgspec_list_model_does_not_leak():
    """`list[Struct]` was excluded by its own guard."""
    body = _serve(list[Public], [Private(id=1, secret="S"), Private(id=2, secret="T")]).json()
    assert body == [{"id": 1}, {"id": 2}]


def test_a_dict_return_is_shaped_to_the_msgspec_model():
    """An undeclared key in a plain dict must not pass either."""
    assert _serve(Public, {"id": 1, "secret": "TOPSECRET"}).json() == {"id": 1}


# ── the declared model is still served correctly ─────────────────────


def test_an_exact_msgspec_instance_round_trips():
    """The negative direction: shaping must not damage a conforming value."""
    assert _serve(Public, Public(id=7)).json() == {"id": 7}


def test_an_exact_msgspec_list_round_trips():
    assert _serve(list[Public], [Public(id=1), Public(id=2)]).json() == [{"id": 1}, {"id": 2}]


def test_an_empty_msgspec_list_is_still_a_list():
    assert _serve(list[Public], []).json() == []


def test_a_route_with_no_response_model_is_untouched():
    """Nothing declared means nothing filtered - the opt-out must stay."""
    app = Veloce(openapi_url=None)

    @app.get("/r")
    async def route():
        return Private(id=1, secret="VISIBLE")

    assert TestClient(app).get("/r").json() == {"id": 1, "secret": "VISIBLE"}


def test_a_multi_field_struct_keeps_every_declared_field():
    class Wide(msgspec.Struct):
        a: int
        b: str
        c: bool

    assert _serve(Wide, Wide(a=1, b="x", c=True)).json() == {"a": 1, "b": "x", "c": True}


def test_an_optional_field_survives_as_null():
    class WithOptional(msgspec.Struct):
        id: int
        note: str | None = None

    assert _serve(WithOptional, WithOptional(id=1)).json() == {"id": 1, "note": None}


# ── a non-conforming value is refused, not silently emitted ──────────


def test_a_value_missing_a_required_field_is_an_error():
    """A shaper that silently emitted a partial object would be worse than the
    leak it replaced."""
    app = Veloce(openapi_url=None)

    @app.get("/r", response_model=Public)
    async def route():
        return {"wrong_field": 1}

    assert TestClient(app).get("/r").status_code == 500


def test_a_value_of_the_wrong_type_is_an_error():
    app = Veloce(openapi_url=None)

    @app.get("/r", response_model=Public)
    async def route():
        return {"id": "not-an-int"}

    assert TestClient(app).get("/r").status_code == 500


# ── the shared shaper handles every backend ──────────────────────────


def test_the_shaper_filters_a_msgspec_struct():
    """The docstring promised "one shaper for every backend"; it raised on this."""
    from veloce._model_backend import shape_through_model

    assert shape_through_model(Private(id=1, secret="S"), Public) == {"id": 1}


def test_the_shaper_filters_a_pydantic_model():
    from veloce._model_backend import shape_through_model

    assert shape_through_model(PPrivate(id=1, secret="S"), PPublic) == {"id": 1}


def test_the_shaper_filters_a_dataclass():
    """The third advertised backend."""
    from dataclasses import dataclass

    from veloce._model_backend import shape_through_model

    @dataclass
    class Narrow:
        id: int

    assert shape_through_model({"id": 1, "extra": "x"}, Narrow) == {"id": 1}


def test_the_shaper_raises_on_a_non_conforming_value():
    from veloce._model_backend import shape_through_model

    with pytest.raises(Exception):
        shape_through_model({"nope": 1}, Public)


def test_the_shaper_accepts_a_plain_mapping():
    from veloce._model_backend import shape_through_model

    assert shape_through_model({"id": 3}, Public) == {"id": 3}


# ── the derived classification survives every route-copy path ────────
#
# `response_model_origin` and `response_model_backend` are computed in
# `RouteInfo.__init__` so the per-response path does not call `get_origin`
# (457 ns) and `backend_of` (624 ns) on every response. A route reaches the tree
# three ways and the last two rebuild the `RouteInfo`, so a derived field is only
# correct if each rebuild recomputes it. They are exempt from the static parity
# guard for that reason - these are the tests that make the exemption honest.


def _blueprint_app():
    from veloce import Blueprint

    bp = Blueprint("shop", url_prefix="/shop")

    @bp.get("/r", response_model=Public)
    async def route():
        return Private(id=1, secret="TOPSECRET")

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)
    return app


def _included_app():
    from veloce import Router

    router = Router()

    @router.get("/v", response_model=Public)
    async def route():
        return Private(id=2, secret="TOPSECRET")

    app = Veloce(openapi_url=None)
    app.include_router(router, prefix="/api")
    return app


def _route_info(app, template):
    for _method, _path, info in app._collect_all_routes(include_hidden=True):
        if info.path_template == template:
            return info
    raise AssertionError(template)


def test_a_blueprint_route_recomputes_the_classification():
    info = _route_info(_blueprint_app(), "/shop/r")
    assert info.response_model_origin is None
    assert info.response_model_backend is not None


def test_a_blueprint_route_still_filters_the_leak():
    assert TestClient(_blueprint_app()).get("/shop/r").json() == {"id": 1}


def test_an_included_router_route_recomputes_the_classification():
    info = _route_info(_included_app(), "/api/v")
    assert info.response_model_origin is None
    assert info.response_model_backend is not None


def test_an_included_router_route_still_filters_the_leak():
    assert TestClient(_included_app()).get("/api/v").json() == {"id": 2}


def test_a_list_model_records_its_origin():
    app = Veloce(openapi_url=None)

    @app.get("/l", response_model=list[Public])
    async def route():
        return [Public(id=1)]

    assert _route_info(app, "/l").response_model_origin is list


def test_a_route_with_no_model_records_no_classification():
    app = Veloce(openapi_url=None)

    @app.get("/n")
    async def route() -> dict:
        return {}

    info = _route_info(app, "/n")
    assert info.response_model_origin is None
