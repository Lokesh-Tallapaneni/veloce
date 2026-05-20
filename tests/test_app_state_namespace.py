"""app.state as a State namespace."""

from __future__ import annotations

from veloce import Veloce
from veloce.http.request import State


def test_app_state_is_State_instance():
    assert isinstance(Veloce().state, State)


def test_app_state_attribute_storage():
    app = Veloce()
    app.state.db = {"connected": True}
    assert app.state.db == {"connected": True}


def test_app_state_dict_storage_still_works():
    """Existing dict-style call sites keep working."""
    app = Veloce()
    app.state["db_url"] = "postgres://localhost/x"
    assert app.state["db_url"] == "postgres://localhost/x"
    assert app.state.get("db_url") == "postgres://localhost/x"


def test_app_state_mixed_access():
    app = Veloce()
    app.state.a = 1
    app.state["b"] = 2
    assert app.state["a"] == 1
    assert app.state.b == 2


def test_app_state_starts_empty():
    assert Veloce().state == {}


def test_app_state_isolated_per_app():
    a = Veloce()
    b = Veloce()
    a.state.x = 1
    assert "x" not in b.state
