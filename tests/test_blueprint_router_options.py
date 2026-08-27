"""A `Blueprint` accepts the router options it is a router for.

`Blueprint` extends `Router`, but its `__init__` forwarded only five of the
seven options `Router` takes. `tags` and `on_duplicate` were dropped, so
`Blueprint("admin", tags=["admin"])` was a `TypeError` and every blueprint had
the default duplicate policy no matter what the app was configured with.

The `on_duplicate` half was worse than a missing feature. Registering the same
path twice on a blueprint raised an error whose text says to pass
`on_duplicate='override'` — a keyword `Blueprint` would then refuse. The
message sent the reader to a dead end.

Both are forwarded now, along with `Router`'s validation of the policy value.
"""

from __future__ import annotations

import logging

import pytest

from tests._openapi import document
from veloce import Blueprint, Depends, JSONResponse, Veloce
from veloce.exceptions import DuplicateRouteError
from veloce.testclient import TestClient

# ── the options exist ────────────────────────────────────────────────


@pytest.mark.parametrize(("option", "value"), [("tags", ["admin"]), ("on_duplicate", "override")])
def test_the_option_is_accepted(option, value):
    """The defect: both raised TypeError."""
    assert Blueprint("bp", **{option: value}) is not None


def test_the_options_reach_the_router():
    bp = Blueprint("bp", tags=["admin"], on_duplicate="override")
    assert bp.tags == ["admin"]
    assert bp.on_duplicate == "override"


def test_the_defaults_are_unchanged():
    bp = Blueprint("bp")
    assert bp.tags == []
    assert bp.on_duplicate == "error"


def test_the_existing_positional_order_still_works():
    """The new options were appended, so no caller's positional args moved."""
    bp = Blueprint("bp", "/p", None, None, None)
    assert bp.name == "bp"
    assert bp.url_prefix == "/p"
    assert bp.on_duplicate == "error"


# ── the duplicate policy ─────────────────────────────────────────────


def test_the_default_policy_still_refuses_a_duplicate():
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.get("/dup")
    async def first() -> dict:
        return {"v": "first"}

    with pytest.raises(DuplicateRouteError):

        @bp.get("/dup")
        async def second() -> dict:
            return {"v": "second"}


def test_override_replaces_the_handler():
    """The defect, end to end: this is what the error message told you to do."""
    bp = Blueprint("bp", url_prefix="/bp", on_duplicate="override")

    @bp.get("/dup")
    async def first() -> dict:
        return {"v": "first"}

    @bp.get("/dup")
    async def second() -> dict:
        return {"v": "second"}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)
    assert TestClient(app).get("/bp/dup").json() == {"v": "second"}


def test_warn_replaces_the_handler_and_logs(caplog):
    bp = Blueprint("bp", url_prefix="/bp", on_duplicate="warn")

    @bp.get("/dup")
    async def first() -> dict:
        return {"v": "first"}

    with caplog.at_level(logging.WARNING):

        @bp.get("/dup")
        async def second() -> dict:
            return {"v": "second"}

    assert any("/bp/dup" in record.getMessage() for record in caplog.records)

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)
    assert TestClient(app).get("/bp/dup").json() == {"v": "second"}


def test_the_error_message_names_a_keyword_that_now_works():
    """The point of the fix: the advice in the message is followable."""
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.get("/dup")
    async def first() -> dict:
        return {}

    with pytest.raises(DuplicateRouteError) as caught:

        @bp.get("/dup")
        async def second() -> dict:
            return {}

    assert "on_duplicate='override'" in str(caught.value)
    # Following it no longer raises TypeError.
    assert Blueprint("bp2", on_duplicate="override").on_duplicate == "override"


@pytest.mark.parametrize("policy", ["error", "warn", "override"])
def test_every_documented_policy_is_accepted(policy):
    assert Blueprint("bp", on_duplicate=policy).on_duplicate == policy


@pytest.mark.parametrize("policy", ["nope", "Override", "", "ERROR", "replace"])
def test_an_unknown_policy_is_refused_at_construction(policy):
    """`Router`'s validation is inherited, so a typo fails where it is written."""
    with pytest.raises(ValueError, match="on_duplicate must be one of"):
        Blueprint("bp", on_duplicate=policy)


def test_a_different_method_on_the_same_path_is_not_a_duplicate():
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.get("/thing")
    async def read() -> dict:
        return {}

    @bp.post("/thing")
    async def write() -> dict:
        return {}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)
    client = TestClient(app)
    assert client.get("/bp/thing").status_code == 200
    assert client.post("/bp/thing").status_code == 200


def test_a_blueprint_policy_does_not_leak_to_another_blueprint():
    lenient = Blueprint("lenient", url_prefix="/a", on_duplicate="override")
    strict = Blueprint("strict", url_prefix="/b")

    @lenient.get("/x")
    async def one() -> dict:
        return {}

    @lenient.get("/x")
    async def two() -> dict:
        return {}

    @strict.get("/x")
    async def three() -> dict:
        return {}

    with pytest.raises(DuplicateRouteError):

        @strict.get("/x")
        async def four() -> dict:
            return {}


def test_a_nested_blueprint_keeps_its_own_policy():
    """It governs registration on the child, which happens before the merge."""
    parent = Blueprint("p", url_prefix="/p", on_duplicate="override")
    child = Blueprint("c", url_prefix="/c")
    parent.register_blueprint(child)
    assert child.on_duplicate == "error"


def test_the_app_policy_still_governs_a_merge_collision():
    """Unchanged: the app owns the final table, so its policy decides."""
    app = Veloce(openapi_url=None, on_duplicate="override")

    @app.get("/x")
    async def from_app() -> dict:
        return {"v": "app"}

    bp = Blueprint("bp")

    @bp.get("/x")
    async def from_bp() -> dict:
        return {"v": "bp"}

    app.register_blueprint(bp)
    assert TestClient(app).get("/x").json() == {"v": "bp"}


def test_a_merge_collision_still_fails_under_the_default_app_policy():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def from_app() -> dict:
        return {}

    bp = Blueprint("bp")

    @bp.get("/x")
    async def from_bp() -> dict:
        return {}

    with pytest.raises(DuplicateRouteError):
        app.register_blueprint(bp)


# ── the tags ─────────────────────────────────────────────────────────


def test_blueprint_tags_reach_the_schema():
    bp = Blueprint("bp", url_prefix="/bp", tags=["admin"])

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app = Veloce()
    app.register_blueprint(bp)
    assert document(app)["paths"]["/bp/x"]["get"]["tags"] == ["admin"]


def test_a_route_tag_is_kept_alongside_the_blueprint_tag():
    bp = Blueprint("bp", url_prefix="/bp", tags=["admin"])

    @bp.get("/x", tags=["reports"])
    async def x() -> dict:
        return {}

    app = Veloce()
    app.register_blueprint(bp)
    assert set(document(app)["paths"]["/bp/x"]["get"]["tags"]) == {"admin", "reports"}


def test_several_blueprint_tags_all_apply():
    bp = Blueprint("bp", url_prefix="/bp", tags=["admin", "internal"])

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app = Veloce()
    app.register_blueprint(bp)
    assert set(document(app)["paths"]["/bp/x"]["get"]["tags"]) == {"admin", "internal"}


def test_a_blueprint_without_tags_adds_none():
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app = Veloce()
    app.register_blueprint(bp)
    assert "tags" not in document(app)["paths"]["/bp/x"]["get"]


def test_a_parent_blueprint_tag_reaches_a_nested_route():
    parent = Blueprint("p", url_prefix="/p", tags=["parent"])
    child = Blueprint("c", url_prefix="/c")

    @child.get("/x")
    async def x() -> dict:
        return {}

    parent.register_blueprint(child)
    app = Veloce()
    app.register_blueprint(parent)
    assert document(app)["paths"]["/p/c/x"]["get"]["tags"] == ["parent"]


def test_a_tag_list_is_not_shared_between_blueprints():
    """A mutable default would alias every blueprint's tags together."""
    first = Blueprint("a")
    second = Blueprint("b")
    first.tags.append("leaked")
    assert second.tags == []


def test_the_caller_s_tag_list_is_not_aliased_by_the_blueprint():
    supplied = ["admin"]
    bp = Blueprint("bp", tags=supplied)
    bp.tags.append("added")
    assert supplied == ["admin"]


# ── the two options together ─────────────────────────────────────────


def test_both_options_apply_to_the_same_blueprint():
    bp = Blueprint("bp", url_prefix="/bp", tags=["admin"], on_duplicate="override")

    @bp.get("/x")
    async def first() -> dict:
        return {"v": "first"}

    @bp.get("/x")
    async def second() -> dict:
        return {"v": "second"}

    app = Veloce()
    app.register_blueprint(bp)
    assert TestClient(app).get("/bp/x").json() == {"v": "second"}
    assert document(app)["paths"]["/bp/x"]["get"]["tags"] == ["admin"]


def test_the_other_router_options_still_forward():
    """The change edits the `super().__init__` call; nothing else may shift."""

    async def dep() -> str:
        return "d"

    bp = Blueprint(
        "bp",
        url_prefix="/bp",
        default_response_class=JSONResponse,
        dependencies=[Depends(dep)],
        responses={404: {"description": "gone"}},
        tags=["admin"],
    )
    assert bp.prefix == "/bp"
    assert bp.default_response_class is JSONResponse
    assert len(bp.router_dependencies) == 1
    assert bp.router_responses == {404: {"description": "gone"}}
