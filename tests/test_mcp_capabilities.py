"""MCP capability objects - base contract, advertisement, and dispatch wiring."""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp.capabilities import (
    Capability,
    LoggingCapability,
    PromptsCapability,
    ResourcesCapability,
    ToolsCapability,
)
from veloce.contrib.mcp.server import MCPServer


def _server(app: Veloce) -> MCPServer:
    return MCPServer(app)


# -- Base contract -----------------------------------------------------


def test_base_hooks_raise_not_implemented():
    """The abstract hooks signal an incomplete subclass via `NotImplementedError`."""

    class Bare(Capability):
        __slots__ = ()

    bare = Bare()
    with pytest.raises(NotImplementedError):
        bare.advertise()
    with pytest.raises(NotImplementedError):
        bare.handlers()


def test_subclass_without_slots_is_rejected():
    """A subclass forgetting `__slots__` fails loudly at class creation."""
    with pytest.raises(TypeError):

        class Leaky(Capability):  # no __slots__
            pass


def test_concretes_are_slotted_and_carry_no_instance_dict():
    """Each concrete capability stays slotted, gaining no per-instance `__dict__`."""
    app = Veloce()
    server = _server(app)
    for cap in (
        ToolsCapability(server),
        ResourcesCapability(server),
        PromptsCapability(server),
        LoggingCapability(server),
    ):
        assert not hasattr(cap, "__dict__")


# -- Advertisement -----------------------------------------------------


def test_tools_and_logging_advertise_unconditionally():
    """A bare app still advertises the always-present tools and logging areas."""
    server = _server(Veloce())
    assert ToolsCapability(server).advertise() == {"tools": {"listChanged": False}}
    assert LoggingCapability(server).advertise() == {"logging": {}}


def test_resources_advertisement_is_gated_on_exposure():
    """Resources advertise only when the app exposes at least one resource."""
    bare = _server(Veloce())
    assert ResourcesCapability(bare).advertise() is None

    app = Veloce()

    @app.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_description="A doc",
        mcp_resource_uri="config://doc",
    )
    async def doc() -> dict:
        return {"ok": True}

    populated = _server(app)
    assert ResourcesCapability(populated).advertise() == {
        "resources": {"subscribe": False, "listChanged": False}
    }


def test_prompts_advertisement_is_gated_on_exposure():
    """Prompts advertise only when the app exposes at least one prompt."""
    bare = _server(Veloce())
    assert PromptsCapability(bare).advertise() is None

    app = Veloce()

    @app.mcp_prompt(description="A greeting")
    async def greet() -> str:
        return "hi"

    populated = _server(app)
    assert PromptsCapability(populated).advertise() == {"prompts": {"listChanged": False}}


# -- Dispatch wiring ---------------------------------------------------


def test_capability_handlers_populate_the_dispatch_map():
    """Every capability method is registered in the server's dispatch map."""
    server = _server(Veloce())
    expected = {
        "tools/list",
        "tools/call",
        "resources/list",
        "resources/templates/list",
        "resources/read",
        "prompts/list",
        "prompts/get",
        "logging/setLevel",
    }
    assert expected <= set(server._methods)
    # Lifecycle methods stay registered alongside the capability methods.
    assert {"initialize", "ping", "notifications/initialized"} <= set(server._methods)
