"""MCP registration lives in its own `app/` mixin, like every other concern.

`app/` is a package of focused mixins: HTTP dispatch, ASGI transport, exception
handling, hooks and lifespan, mounting, middleware, OpenAPI, test clients,
native serving, templating and background tasks each have one. MCP was 347
lines of `core.py` instead — the one subsystem that had grown past the boundary
the architecture already draws.

Moving methods between a class and a base it inherits is invisible at runtime
until it isn't: a name that collides with another mixin, or a method the MRO
resolves elsewhere, would change behaviour silently. These pin the wiring.
"""

from __future__ import annotations

import inspect

import pytest

from veloce import Veloce
from veloce.app.mcp import MCPMixin

#: Everything the mixin is responsible for, including the state initialiser
#: `_init_runtime_state` delegates to it.
_MCP_METHODS = [
    "mcp_tool",
    "mcp_prompt",
    "mcp_completer",
    "before_mcp_call",
    "after_mcp_call",
    "add_mcp_tool",
    "mount_mcp",
    "_init_mcp_state",
]


@pytest.mark.parametrize("name", _MCP_METHODS)
def test_the_app_resolves_each_method_to_the_mixin(name):
    """Not merely present - present *from here*, so no other base shadows it."""
    assert getattr(Veloce, name) is getattr(MCPMixin, name)


def test_the_mixin_defines_nothing_another_app_mixin_also_defines():
    """A silent MRO collision is exactly what this move could have introduced."""
    own = {n for n, _ in inspect.getmembers(MCPMixin, inspect.isfunction)}
    collisions = {}
    for base in Veloce.__mro__:
        if base in (MCPMixin, object):
            continue
        shared = own & {
            n for n, v in vars(base).items() if inspect.isfunction(v) and not n.startswith("__")
        }
        if shared:
            collisions[base.__name__] = sorted(shared)
    assert not collisions, f"MCPMixin methods also defined elsewhere: {collisions}"


def test_no_mixin_declares_slots():
    """`Veloce` is unslotted by design, so a mixin's `__slots__` says nothing.

    Four of the thirteen mixins declared `__slots__ = ()` and nine did not, and
    a test here asserted the four were right - behind a docstring claiming a
    mixin without it "would give every app a `__dict__`". That is not what
    happens: `Router` and most of the mixins are unslotted, so an app has a
    `__dict__` either way, which the last assertion below shows. Nor could the
    nine adopt it: they assign to `self` (they write the host's state), which a
    slotted class refuses.
    """
    import importlib
    import pkgutil

    import veloce.app as app_package

    mixins: list[type] = []
    for info in pkgutil.iter_modules(app_package.__path__):
        module = importlib.import_module(f"veloce.app.{info.name}")
        mixins += [
            obj
            for _name, obj in inspect.getmembers(module, inspect.isclass)
            if obj.__name__.endswith("Mixin") and obj.__module__ == module.__name__
        ]
    assert len(mixins) >= 13, f"the scan found only {len(mixins)} mixins - it is not running"

    declaring = sorted(m.__name__ for m in mixins if "__slots__" in m.__dict__)
    assert declaring == [], f"these mixins declare a __slots__ that has no effect: {declaring}"

    app = Veloce(openapi_url=None)
    app.an_attribute_no_slot_declares = 1
    assert app.an_attribute_no_slot_declares == 1


def test_the_mcp_registries_are_initialised_on_a_plain_app():
    """`_init_runtime_state` delegates here; an app must still come up ready."""
    app = Veloce(openapi_url=None)
    assert app._mcp_tools == []
    assert app._mcp_prompts == []
    assert app._mcp_completers == []
    assert app._mcp_before_call == []
    assert app._mcp_after_call == []


def test_registration_still_reaches_those_registries():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="A tool")
    async def a_tool() -> dict:
        return {"ok": True}

    @app.mcp_prompt(description="A prompt")
    async def a_prompt() -> str:
        return "hi"

    @app.before_mcp_call
    async def before(ctx) -> None:
        return None

    @app.after_mcp_call
    async def after(ctx, result) -> None:
        return None

    assert len(app._mcp_tools) == 1
    assert len(app._mcp_prompts) == 1
    assert len(app._mcp_before_call) == 1
    assert len(app._mcp_after_call) == 1


def test_an_app_that_registers_nothing_never_loads_the_subsystem():
    """The mixin's `contrib.mcp` imports stay inside the methods that need them."""
    import subprocess
    import sys

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, veloce; veloce.Veloce(openapi_url=None); "
            "print('veloce.contrib.mcp' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False"
