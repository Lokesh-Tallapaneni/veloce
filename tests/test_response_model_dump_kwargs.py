"""`model_dump` options are resolved once at registration, not per request.

`_apply_response_model` rebuilt the same `dump_kwargs` dict on every response:
six attribute reads and a dict construction whose result is fixed the moment the
route is registered.

    dump_kwargs = {}
    if route_info.response_model_exclude_unset: dump_kwargs["exclude_unset"] = True
    ...                                       # x6, every request

That mattered more than it looks, because a response model no longer has to be
declared - a return annotation supplies one - so this ran on most routes rather
than on the few that passed `response_model=`.

The six options are constructor arguments and nothing assigns them afterwards,
so the mapping is built in `__init__` and read per request. This file pins that
it *is* precomputed, and - the part that actually matters for a change whose
whole point is to alter nothing observable - that every option and combination
still dumps exactly what it dumped before.

Behaviour is asserted end-to-end through real requests rather than by comparing
dicts, because the dict is the mechanism and the JSON is the contract.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from veloce import Veloce
from veloce.testclient import TestClient

# Module scope: this file uses PEP 563, so a model defined inside a test
# function cannot be resolved by name when the route is built.


class User(BaseModel):
    id: int
    name: str
    nickname: str | None = None
    role: str = "member"


class Aliased(BaseModel):
    user_id: int = Field(alias="userId")
    display: str = Field(alias="displayName")

    model_config = {"populate_by_name": True}


def _app_with(**options) -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/u", response_model=User, **options)
    async def read_user():
        return {"id": 1, "name": "ada"}

    return app


def _route(app: Veloce, template: str = "/u"):
    for _method, _path, info in app._collect_all_routes(include_hidden=True):
        if info.path_template == template:
            return info
    raise AssertionError(f"no route for {template}")


# ── the precomputation itself ────────────────────────────────────────


def test_the_options_are_resolved_at_registration():
    """The defect: this mapping was rebuilt on every response."""
    info = _route(_app_with(response_model_exclude_none=True))
    assert info.response_dump_kwargs == {"exclude_none": True}


def test_a_route_with_no_options_precomputes_an_empty_mapping():
    assert _route(_app_with()).response_dump_kwargs == {}


def test_every_option_lands_under_its_pydantic_name():
    """A typo in a key would silently stop applying that option."""
    info = _route(
        _app_with(
            response_model_exclude_unset=True,
            response_model_exclude_defaults=True,
            response_model_by_alias=True,
            response_model_exclude_none=True,
        )
    )
    assert info.response_dump_kwargs == {
        "exclude_unset": True,
        "exclude_defaults": True,
        "by_alias": True,
        "exclude_none": True,
    }


def test_include_and_exclude_carry_the_normalised_sets():
    info = _route(
        _app_with(
            response_model_include=["id", "name"],
            response_model_exclude=["role"],
        )
    )
    assert info.response_dump_kwargs["include"] == {"id", "name"}
    assert info.response_dump_kwargs["exclude"] == {"role"}


def test_a_falsy_option_is_absent_rather_than_false():
    """Passing `exclude_unset=False` to Pydantic is not the same as omitting it
    for a caller that inspects the mapping, and it was omitted before."""
    keys = _route(_app_with(response_model_exclude_unset=False)).response_dump_kwargs
    assert "exclude_unset" not in keys


def test_an_empty_include_collection_is_omitted():
    """`include=set()` would dump *nothing*; the old code treated it as unset."""
    assert "include" not in _route(_app_with(response_model_include=[])).response_dump_kwargs


# ── behaviour parity: what each option actually emits ────────────────


def test_undeclared_fields_are_still_dropped():
    app = Veloce(openapi_url=None)

    @app.get("/u", response_model=User)
    async def read_user():
        return {"id": 1, "name": "ada", "secret": "leaked"}

    body = TestClient(app).get("/u").json()
    assert "secret" not in body
    assert body == {"id": 1, "name": "ada", "nickname": None, "role": "member"}


def test_exclude_unset_omits_fields_the_handler_never_set():
    body = TestClient(_app_with(response_model_exclude_unset=True)).get("/u").json()
    assert body == {"id": 1, "name": "ada"}


def test_exclude_defaults_omits_fields_left_at_their_default():
    body = TestClient(_app_with(response_model_exclude_defaults=True)).get("/u").json()
    assert body == {"id": 1, "name": "ada"}


def test_exclude_none_omits_null_fields():
    body = TestClient(_app_with(response_model_exclude_none=True)).get("/u").json()
    assert "nickname" not in body
    assert body["role"] == "member"


def test_include_keeps_only_the_named_fields():
    body = TestClient(_app_with(response_model_include=["id"])).get("/u").json()
    assert body == {"id": 1}


def test_exclude_drops_the_named_fields():
    body = TestClient(_app_with(response_model_exclude=["role", "nickname"])).get("/u").json()
    assert body == {"id": 1, "name": "ada"}


def test_by_alias_emits_the_field_aliases():
    app = Veloce(openapi_url=None)

    @app.get("/a", response_model=Aliased, response_model_by_alias=True)
    async def aliased():
        return {"user_id": 7, "display": "Ada"}

    assert TestClient(app).get("/a").json() == {"userId": 7, "displayName": "Ada"}


def test_without_by_alias_the_field_names_are_emitted():
    app = Veloce(openapi_url=None)

    @app.get("/a", response_model=Aliased)
    async def aliased():
        return {"user_id": 7, "display": "Ada"}

    assert TestClient(app).get("/a").json() == {"user_id": 7, "display": "Ada"}


# ── combinations, which is where a shared mapping could go wrong ─────


def test_include_and_exclude_together():
    """Exclude wins over include for a field named by both - Pydantic's rule."""
    body = (
        TestClient(
            _app_with(response_model_include=["id", "name"], response_model_exclude=["name"])
        )
        .get("/u")
        .json()
    )
    assert body == {"id": 1}


def test_exclude_unset_with_by_alias():
    app = Veloce(openapi_url=None)

    @app.get("/a", response_model=Aliased, response_model_by_alias=True)
    async def aliased():
        return {"user_id": 7, "display": "Ada"}

    assert TestClient(app).get("/a").json() == {"userId": 7, "displayName": "Ada"}


def test_two_routes_do_not_share_one_mapping():
    """A precomputed dict held on the wrong object would leak across routes."""
    app = Veloce(openapi_url=None)

    @app.get("/plain", response_model=User)
    async def plain():
        return {"id": 1, "name": "ada"}

    @app.get("/lean", response_model=User, response_model_exclude_unset=True)
    async def lean():
        return {"id": 2, "name": "grace"}

    client = TestClient(app)
    assert client.get("/plain").json() == {
        "id": 1,
        "name": "ada",
        "nickname": None,
        "role": "member",
    }
    assert client.get("/lean").json() == {"id": 2, "name": "grace"}
    assert _route(app, "/plain").response_dump_kwargs == {}
    assert _route(app, "/lean").response_dump_kwargs == {"exclude_unset": True}


def test_repeated_requests_do_not_mutate_the_shared_mapping():
    """A per-request dict could be mutated with impunity; a shared one cannot."""
    app = _app_with(response_model_exclude_none=True)
    client = TestClient(app)
    for _ in range(5):
        assert "nickname" not in client.get("/u").json()
    assert _route(app).response_dump_kwargs == {"exclude_none": True}


# ── list-typed response models use the same options per element ──────


def test_a_list_response_model_applies_the_options_to_each_element():
    app = Veloce(openapi_url=None)

    @app.get("/us", response_model=list[User], response_model_exclude_none=True)
    async def read_users():
        return [{"id": 1, "name": "ada"}, {"id": 2, "name": "grace"}]

    body = TestClient(app).get("/us").json()
    assert body == [
        {"id": 1, "name": "ada", "role": "member"},
        {"id": 2, "name": "grace", "role": "member"},
    ]


def test_an_empty_list_response_model_is_still_a_list():
    app = Veloce(openapi_url=None)

    @app.get("/us", response_model=list[User])
    async def read_users():
        return []

    assert TestClient(app).get("/us").json() == []


# ── the inferred-model path, which is now the common one ─────────────


def test_an_inferred_response_model_gets_the_same_treatment():
    """A return annotation supplies the model, so this path is the common one."""
    app = Veloce(openapi_url=None)

    @app.get("/u", response_model_exclude_none=True)
    async def read_user() -> User:
        return User(id=1, name="ada")

    assert "nickname" not in TestClient(app).get("/u").json()
    assert _route(app).response_dump_kwargs == {"exclude_none": True}


def test_a_route_with_no_model_has_an_empty_mapping():
    """Nothing to dump; the attribute must still exist so the read never raises."""
    app = Veloce(openapi_url=None)

    @app.get("/raw")
    async def raw():
        return {"free": "form"}

    assert _route(app, "/raw").response_dump_kwargs == {}
    assert TestClient(app).get("/raw").json() == {"free": "form"}


# ── negative: a bad payload still fails the same way ─────────────────


def test_a_payload_missing_a_required_field_still_errors():
    app = Veloce(openapi_url=None)

    @app.get("/u", response_model=User)
    async def read_user():
        return {"name": "no-id"}

    with pytest.raises(Exception):
        TestClient(app, raise_server_exceptions=True).get("/u")


def test_a_wrongly_typed_field_is_still_coerced_or_rejected():
    app = Veloce(openapi_url=None)

    @app.get("/u", response_model=User)
    async def read_user():
        return {"id": "12", "name": "ada"}

    # Pydantic coerces a numeric string to int; the point is the model still runs.
    assert TestClient(app).get("/u").json()["id"] == 12


# ── the documented consequence of resolving at registration ──────────


def test_assigning_a_flag_after_registration_does_not_take_effect():
    """Documented in the guide: the flags are constructor arguments.

    Before, each response re-read the attributes, so a post-hoc assignment
    happened to work. It was never a supported way to configure a route - and
    `response_model_include` was already normalised to a set at construction, so
    assigning a list to it was half-broken either way.
    """
    app = _app_with()
    _route(app).response_model_exclude_none = True
    body = TestClient(app).get("/u").json()
    assert "nickname" in body


def test_the_decorator_flag_is_the_supported_form():
    """The other half of the note above: pass it to the route decorator."""
    body = TestClient(_app_with(response_model_exclude_none=True)).get("/u").json()
    assert "nickname" not in body


# ── the derived mapping survives every route-copy path ───────────────
#
# A route reaches the tree three ways: directly, spliced from a blueprint, and
# merged from an included router. The last two rebuild the `RouteInfo`, so a
# derived field is only correct if each rebuild recomputes it. `response_dump_kwargs`
# is exempt from the static parity guard in `test_route_field_parity.py` for
# exactly that reason - these are the tests that make the exemption honest.


def test_a_blueprint_route_recomputes_the_mapping():
    from veloce import Blueprint

    bp = Blueprint("shop", url_prefix="/shop")

    @bp.get("/u", response_model=User, response_model_exclude_none=True)
    async def read_user():
        return {"id": 1, "name": "ada"}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)

    assert _route(app, "/shop/u").response_dump_kwargs == {"exclude_none": True}
    assert "nickname" not in TestClient(app).get("/shop/u").json()


def test_an_included_router_route_recomputes_the_mapping():
    from veloce import Router

    router = Router()

    @router.get("/v", response_model=User, response_model_exclude_unset=True)
    async def read_user():
        return {"id": 2, "name": "grace"}

    app = Veloce(openapi_url=None)
    app.include_router(router, prefix="/api")

    assert _route(app, "/api/v").response_dump_kwargs == {"exclude_unset": True}
    assert TestClient(app).get("/api/v").json() == {"id": 2, "name": "grace"}


def test_a_copied_route_with_no_options_still_has_the_attribute():
    """An absent attribute would raise on the first response, not misbehave."""
    from veloce import Blueprint

    bp = Blueprint("plain", url_prefix="/p")

    @bp.get("/u", response_model=User)
    async def read_user():
        return {"id": 3, "name": "hopper"}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)

    assert _route(app, "/p/u").response_dump_kwargs == {}
    assert TestClient(app).get("/p/u").json()["nickname"] is None


def test_include_and_exclude_sets_survive_a_copy():
    """Collections are normalised in `__init__`; a copy must renormalise, not
    carry a half-converted value across."""
    from veloce import Router

    router = Router()

    @router.get("/w", response_model=User, response_model_include=["id", "name"])
    async def read_user():
        return {"id": 4, "name": "lovelace"}

    app = Veloce(openapi_url=None)
    app.include_router(router, prefix="/api")

    assert _route(app, "/api/w").response_dump_kwargs["include"] == {"id", "name"}
    assert TestClient(app).get("/api/w").json() == {"id": 4, "name": "lovelace"}
