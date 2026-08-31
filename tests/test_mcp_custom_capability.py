"""An out-of-tree `Capability` can actually be served.

`Capability` is exported from two gateways and its own docstring calls it "the
documented seam an out-of-tree capability implements against". That was not
true: the type its abstract `handlers()` returns could not be named from public
API, and the capability list was built inside `MCPServer.__init__` with no way
to add to it - so nothing a user could do with the export was supported.

These tests are the promise, driven entirely through public names.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp import Capability, MCPServer, MethodHandler


class PingCapability(Capability):
    """A minimal out-of-tree spec area, written the way a user would write one."""

    __slots__ = ("greeting",)

    def __init__(self, greeting: str = "pong") -> None:
        self.greeting = greeting

    def advertise(self, *, modern: bool = False) -> dict[str, object] | None:
        return {"x-ping": {"modern": modern}}

    def handlers(self) -> dict[str, MethodHandler]:
        async def ping(params: dict[str, object]) -> dict[str, object]:
            return {"reply": self.greeting, "echo": params.get("echo")}

        return {"x-ping/ping": ping}


@pytest.fixture
def server() -> MCPServer:
    return MCPServer(Veloce(openapi_url=None), capabilities=[PingCapability()])


def test_the_method_is_dispatchable(server: MCPServer) -> None:
    assert "x-ping/ping" in server._methods


async def test_the_handler_actually_runs(server: MCPServer) -> None:
    result = await server._methods["x-ping/ping"]({"echo": 7})
    assert result == {"reply": "pong", "echo": 7}


def test_the_capability_is_advertised(server: MCPServer) -> None:
    advertised = [c.advertise() for c in server._capabilities]
    assert {"x-ping": {"modern": False}} in advertised


def test_the_built_in_capabilities_are_still_there(server: MCPServer) -> None:
    """Adding one must not displace the shipped set."""
    plain = MCPServer(Veloce(openapi_url=None))
    assert len(server._capabilities) == len(plain._capabilities) + 1
    for method in plain._methods:
        assert method in server._methods


def test_no_capabilities_argument_changes_nothing() -> None:
    plain = MCPServer(Veloce(openapi_url=None))
    explicit_none = MCPServer(Veloce(openapi_url=None), capabilities=None)
    empty = MCPServer(Veloce(openapi_url=None), capabilities=[])
    assert set(plain._methods) == set(explicit_none._methods) == set(empty._methods)


def test_a_caller_capability_wins_a_method_collision() -> None:
    """Documented: caller-supplied capabilities go last, so an override sticks."""

    class Override(Capability):
        __slots__ = ()

        def advertise(self, *, modern: bool = False) -> dict[str, object] | None:
            return None

        def handlers(self) -> dict[str, MethodHandler]:
            async def replaced(params: dict[str, object]) -> dict[str, object]:
                return {"replaced": True}

            return {"tools/list": replaced}

    server = MCPServer(Veloce(openapi_url=None), capabilities=[Override()])
    assert server._methods["tools/list"].__name__ == "replaced"


def test_a_subclass_forgetting_slots_is_refused() -> None:
    """The base's own discipline still applies to an out-of-tree subclass."""
    with pytest.raises(TypeError, match="__slots__"):

        class Sloppy(Capability):
            def advertise(self, *, modern: bool = False) -> dict[str, object] | None:
                return None

            def handlers(self) -> dict[str, MethodHandler]:
                return {}
