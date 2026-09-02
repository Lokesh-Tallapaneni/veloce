"""`Security(scopes=...)` takes a sequence of scopes, not a string."""

from __future__ import annotations

import pytest

from veloce import Security
from veloce._handler_plan import build_plan


def _authz(request):
    return "ok"


def _scope_lists(handler) -> list[list[str]]:
    return [
        slot.target_type
        for slot in build_plan(handler).slots
        if isinstance(getattr(slot, "target_type", None), list)
    ]


def test_a_list_of_scopes_is_recorded_whole():
    """POSITIVE: the declared scopes reach the plan unchanged."""

    def handler(request, user=Security(_authz, scopes=["read", "write"])):
        return user

    assert ["read", "write"] in _scope_lists(handler)


def test_a_tuple_of_scopes_is_accepted():
    """POSITIVE: any sequence works; only str/bytes are the trap."""

    def handler(request, user=Security(_authz, scopes=("read",))):
        return user

    assert ["read"] in _scope_lists(handler)


def test_a_bare_string_scope_is_refused():
    """NEGATIVE: `scopes="read"` used to become ['r','e','a','d']."""
    with pytest.raises(TypeError, match="sequence of scopes"):
        Security(_authz, scopes="read")


def test_bytes_scopes_are_refused():
    """NEGATIVE: bytes iterate to ints, which is worse than the string case."""
    with pytest.raises(TypeError, match="sequence of scopes"):
        Security(_authz, scopes=b"read")


def test_no_scopes_is_still_valid():
    """POSITIVE: `Security()` without scopes is the common form.

    `__init__` normalises a missing value to `[]`, so that is what is asserted.
    """
    assert Security(_authz).scopes == []
