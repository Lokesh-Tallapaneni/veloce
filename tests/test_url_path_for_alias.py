"""url_path_for alias on Router (R15)."""

from __future__ import annotations

import uuid

import pytest

from veloce import BuildError, Veloce


def _make_app() -> Veloce:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/users/{id}", name="get_user")
    async def get_user(id: str):
        return {"id": id}

    @app.get("/items/{id:int}/edit", name="edit_item")
    async def edit_item(id: int):
        return {}

    @app.get("/files/{u:uuid}", name="get_file")
    async def get_file(u):
        return {}

    return app


# ── url_path_for ──────────────────────────────────────────────────────


def test_url_path_for_simple_substitution():
    app = _make_app()
    assert app.url_path_for("get_user", id="42") == "/users/42"


def test_url_path_for_is_same_callable_as_url_for():
    """The alias points to the canonical method — no behaviour drift."""
    app = _make_app()
    assert app.url_path_for("get_user", id="x") == app.url_for("get_user", id="x")


def test_url_path_for_with_typed_converter():
    """`{id:int}` placeholder strips the `:int` and substitutes the value."""
    app = _make_app()
    assert app.url_path_for("edit_item", id=7) == "/items/7/edit"


def test_url_path_for_with_uuid_converter():
    app = _make_app()
    u = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
    assert app.url_path_for("get_file", u=u) == f"/files/{u}"


def test_url_path_for_int_path_param():
    """Non-string path-param values (int, UUID) are str()-coerced."""
    app = _make_app()
    assert app.url_path_for("get_user", id=42) == "/users/42"


def test_url_path_for_missing_param_raises():

    app = _make_app()
    with pytest.raises(BuildError):
        app.url_path_for("get_user")


def test_url_path_for_unknown_name_raises():

    app = _make_app()
    with pytest.raises(BuildError):
        app.url_path_for("does_not_exist", id="x")


# ── url_for still works (back-compat) ────────────────────────────────


def test_url_for_unchanged():
    app = _make_app()
    assert app.url_for("get_user", id="42") == "/users/42"
    assert app.url_for("edit_item", id=99) == "/items/99/edit"
