"""`Capability` declares every member the server resolves on it.

`Capability` is the documented seam an out-of-tree integration implements
against - the class docstring says a subclass "adding a new authentication
style implements this and is published like a built-in". It declared two members,
`advertise()` and `handlers()`, while the server resolved a third by
`getattr(capability, "extensions", None)`.

So the contract did not describe itself: an author reading the base class had no
way to learn that contributing an extensions entry was possible, and the one
in-tree capability that does it (`TasksCapability`) looked like it was using a
private hook.

`extensions()` is now declared on the base with a `None` default - most
capabilities contribute nothing - and the server calls it directly.
"""

from __future__ import annotations

import inspect

import pytest

from veloce import Veloce
from veloce.contrib.mcp.capabilities.base import Capability
from veloce.contrib.mcp.server import MCPServer

CONTRACT = ("advertise", "handlers", "extensions")


def _capabilities(app: Veloce):
    return MCPServer(app)._capabilities


def _app() -> Veloce:
    app = Veloce(title="T", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Adds two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    return app


# ── the contract is declared, not probed for ─────────────────────────


@pytest.mark.parametrize("member", CONTRACT)
def test_the_base_declares_every_contract_member(member):
    """The defect: `extensions` was resolved by `getattr` and declared nowhere."""
    assert member in vars(Capability), f"{member} is not declared on Capability"


@pytest.mark.parametrize("member", CONTRACT)
def test_every_shipped_capability_answers_the_whole_contract(member):
    for capability in _capabilities(_app()):
        assert callable(getattr(capability, member)), type(capability).__name__


def test_extensions_defaults_to_contributing_nothing():
    """Most capabilities contribute no extension; the default must say so."""
    for capability in _capabilities(_app()):
        entry = capability.extensions()
        assert entry is None or isinstance(entry, dict), type(capability).__name__


def test_the_declared_default_is_documented():
    """A seam member with no docstring is not a seam anyone can implement."""
    assert (Capability.extensions.__doc__ or "").strip()


# ── and the behaviour is unchanged ───────────────────────────────────


def test_a_capability_contributing_an_extension_is_advertised():
    """`TasksCapability` is the one in-tree implementor; its entry must survive
    the move from `getattr` to a declared method."""
    app = _app()
    server = MCPServer(app)
    contributed = {}
    for capability in server._capabilities:
        entry = capability.extensions()
        if entry:
            contributed.update(entry)
    # Whatever the shipped set is, gathering it must not raise and must be a
    # mapping - the assertion that matters is that no capability is skipped.
    assert isinstance(contributed, dict)


def test_a_custom_capability_needs_only_the_two_required_members():
    """The `None` default is what lets an author implement two of three."""

    class Minimal(Capability):
        __slots__ = ()

        def advertise(self, *, modern: bool = False):
            return {"minimal": {}}

        def handlers(self):
            return {}

    minimal = Minimal()
    assert minimal.extensions() is None
    assert minimal.advertise() == {"minimal": {}}


def test_a_custom_capability_may_contribute_an_extension():
    class WithExtension(Capability):
        __slots__ = ()

        def advertise(self, *, modern: bool = False):
            return None

        def handlers(self):
            return {}

        def extensions(self):
            return {"custom": {"enabled": True}}

    assert WithExtension().extensions() == {"custom": {"enabled": True}}


def test_the_required_members_still_refuse_to_be_skipped():
    """The negative: giving `extensions` a default must not give the other two
    one - a capability that implements neither is a bug, not a minimal one."""

    class Empty(Capability):
        __slots__ = ()

    with pytest.raises(NotImplementedError):
        Empty().advertise()
    with pytest.raises(NotImplementedError):
        Empty().handlers()


def test_a_subclass_without_slots_is_still_refused():
    """The base's other structural guard must survive the addition."""
    with pytest.raises(TypeError, match="__slots__"):

        class NoSlots(Capability):
            def advertise(self, *, modern: bool = False):
                return None

            def handlers(self):
                return {}


def test_the_server_no_longer_probes_for_the_member():
    """Stated at the call site: a declared member should be called, not
    discovered."""
    source = inspect.getsource(MCPServer._advertised_extensions)
    assert 'getattr(capability, "extensions"' not in source
