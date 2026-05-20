"""Request.state — attribute access + dict access coexist."""

from __future__ import annotations

import pytest

from veloce import Request
from veloce.http.request import State


def _req() -> Request:
    return Request(method="GET", path="/", query_string="", headers={}, body=b"")


# ── State class ─────────────────────────────────────────────────────


def test_state_is_dict_subclass():
    s = State()
    assert isinstance(s, dict)


def test_state_attribute_set_and_get():
    s = State()
    s.user = "alice"
    assert s.user == "alice"


def test_state_attribute_and_item_are_same_slot():
    s = State()
    s.token = "abc"
    assert s["token"] == "abc"
    s["other"] = 1
    assert s.other == 1


def test_state_missing_attribute_raises_attribute_error():
    s = State()
    with pytest.raises(AttributeError):
        _ = s.nonexistent


def test_state_delattr():
    s = State()
    s.temp = 1
    del s.temp
    assert "temp" not in s


def test_state_delattr_missing_raises():
    s = State()
    with pytest.raises(AttributeError):
        del s.missing


def test_state_get_method_works():
    s = State()
    s.x = 1
    assert s.get("x") == 1
    assert s.get("y", "default") == "default"


# ── Request.state ───────────────────────────────────────────────────


def test_request_state_is_State_instance():
    assert isinstance(_req().state, State)


def test_request_state_attribute_storage():
    req = _req()
    req.state.current_user = {"id": 7}
    assert req.state.current_user == {"id": 7}


def test_request_state_dict_storage_still_works():
    """Existing dict-style call sites keep working."""
    req = _req()
    req.state["request_id"] = "xyz"
    assert req.state["request_id"] == "xyz"
    assert req.state.get("request_id") == "xyz"
    assert req.state.get("absent", 0) == 0


def test_request_state_mixed_access():
    req = _req()
    req.state.a = 1
    req.state["b"] = 2
    assert req.state["a"] == 1
    assert req.state.b == 2
