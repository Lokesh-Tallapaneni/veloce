"""What holds a server together, asserted rather than assumed.

Three seams that were working by luck. `ToolSearch` was handed `self` from the
middle of `MCPServer.__init__`, so six of the ten attributes a maintainer would
reach for were unset and the failure would have been a bare `AttributeError` on
a slotted object. The session context variable lost its type in a move, taking
type checking off every reader with it - including `session.hidden`, added in
the same batch. And a boolean flag was positional on `mount`.
"""

from __future__ import annotations

import inspect

import pytest

from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer


def _app() -> Veloce:
    app = Veloce(title="Seams", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Search the index")
    async def search(query: str) -> dict:
        return {"query": query}

    return app


# ── The search catalogue is built on a whole server ──────────────────


def test_the_search_catalogue_sees_a_fully_constructed_server():
    """It holds the server and calls back into it, so it must get a finished one."""
    seen: dict[str, bool] = {}
    original = MCPServer._describe_tool

    class Probe(MCPServer):
        __slots__ = ()

    server = MCPServer(_app(), tool_search=True)
    for attribute in MCPServer.__slots__:
        seen[attribute] = hasattr(server, attribute)
    assert all(seen.values()), [name for name, ok in seen.items() if not ok]
    assert original is MCPServer._describe_tool


def test_every_slot_is_set_before_the_catalogue_is_built():
    captured: list[list[str]] = []
    from veloce.contrib.mcp import toolsearch

    real_init = toolsearch.ToolSearch.__init__

    def spy(self, server):  # noqa: ANN001 - a test double
        captured.append([name for name in MCPServer.__slots__ if not hasattr(server, name)])
        real_init(self, server)

    toolsearch.ToolSearch.__init__ = spy
    try:
        MCPServer(_app(), tool_search=True)
    finally:
        toolsearch.ToolSearch.__init__ = real_init
    assert captured == [[]], f"unset when the catalogue was built: {captured}"


def test_a_server_without_search_holds_none():
    assert MCPServer(_app())._tool_search is None


# ── The session variable carries its type ────────────────────────────


def test_the_session_variable_is_not_typed_as_any():
    """`Any` silently switched off checking on every reader of `session.hidden`.

    Read off the module's `__annotations__` rather than matched in its source
    text. The previous form asserted the literal string
    `"_session_var: ContextVar[MCPSession | None]"` appeared and
    `"ContextVar[Any]"` did not, which pins the *spelling* - a reformat, a line
    break or an equivalent alias would fail it while the type was still precise,
    and `ContextVar["Any"]` would pass while it was not.
    """
    from veloce.contrib.mcp import context

    annotation = context.__annotations__.get("_session_var")
    assert annotation is not None, "_session_var carries no annotation at all"
    assert "Any" not in str(annotation), annotation
    assert "MCPSession" in str(annotation), annotation


def test_the_server_returns_the_session_precisely_typed():
    """What the absent `cast(` was really about.

    The old test asserted `"cast(" not in inspect.getsource(...)`, which pins the
    absence of a token: it would pass on a body that had been made *less* precise
    some other way, and fail on a `cast` used for an unrelated reason. The
    property is that the return type says exactly what comes back, which is what
    makes a cast unnecessary in the first place.
    """
    annotation = inspect.signature(MCPServer.current_session).return_annotation
    assert annotation is not inspect.Signature.empty
    assert str(annotation) == "MCPSession | None"


# ── A flag reads as a flag ───────────────────────────────────────────


def test_expose_mcp_is_keyword_only():
    """Positionally it is a bare `True` at the call site, naming nothing."""
    parameter = inspect.signature(Veloce.mount).parameters["expose_mcp"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_mounting_positionally_is_refused():
    parent = Veloce(title="Parent", openapi_url=None)
    with pytest.raises(TypeError):
        parent.mount("/child", _app(), True)  # type: ignore[misc]


def test_mounting_by_keyword_still_works():
    parent = Veloce(title="Parent", openapi_url=None)
    parent.mount("/child", _app(), expose_mcp=True)
    from veloce.contrib.mcp.registry import build_registry

    assert "child_search" in build_registry(parent).tools


# ── The pre-built tool list says what it holds ───────────────────────


def test_a_derived_tool_and_a_proxied_tool_share_one_list():
    """Both are handed over already built; the name now covers both."""
    from veloce.contrib.mcp import ArgTransform, derive_tool
    from veloce.contrib.mcp.registry import build_registry

    app = _app()
    app.add_mcp_tool(
        derive_tool(
            build_registry(app).tools["search"],
            name="public_search",
            arguments={"query": ArgTransform(name="q")},
        )
    )
    assert len(app._mcp_prebuilt_tools) == 1
    assert "public_search" in build_registry(app).tools
