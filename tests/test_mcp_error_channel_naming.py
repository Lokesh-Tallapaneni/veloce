"""A test named for the JSON-RPC error channel must assert the error channel.

MCP reports two kinds of failure and the distinction is load-bearing: a
**transport** error goes on the JSON-RPC error channel (`error.code`), while a
**tool execution** error is reported in band (`result.isError`) so the client can
feed it back to the model. Clients act on them differently.

Four tests in `test_mcp.py` were named `..._is_invalid_params`, carried docstrings
saying "invalid-params transport error", and then asserted `result.isError` -
the opposite. One of them had a leading comment stating the wrong thing and a
five-line rebuttal comment pasted above the assertion stating the right thing.
A reader could not tell which was the contract.

They are renamed. This guard keeps the two vocabularies from drifting apart
again: a test whose *name* claims one channel must assert that channel.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession

TESTS = pathlib.Path(__file__).resolve().parent

# Names that claim the JSON-RPC error channel.
TRANSPORT_MARKERS = ("_is_invalid_params", "_is_method_not_found", "_is_parse_error")
# Names that claim the in-band tool result.
IN_BAND_MARKERS = ("_is_an_in_band_tool_error", "_is_in_band")


def _test_functions(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            yield node


def _source_of(path: pathlib.Path, node) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def _mcp_modules():
    return sorted(TESTS.glob("test_mcp*.py"))


def test_there_are_modules_to_check():
    """A glob that matched nothing would make the guards below vacuous."""
    assert len(_mcp_modules()) > 10


def test_a_transport_named_test_does_not_assert_the_in_band_result():
    """The defect: four tests named `..._is_invalid_params` asserted `isError`.

    The check is the *contradiction*, not the mere absence of an `error` key: a
    test may legitimately claim invalid-params by asserting the exception type
    (`pytest.raises(InvalidParamsError)`) rather than a wire envelope, and
    `test_mcp_pagination.py` does exactly that. What must never happen is a name
    claiming the transport channel over a body asserting the in-band one.
    """
    offenders = []
    for path in _mcp_modules():
        for node in _test_functions(path):
            if not any(marker in node.name for marker in TRANSPORT_MARKERS):
                continue
            if "isError" in _source_of(path, node):
                offenders.append(f"{path.name}::{node.name}")
    assert not offenders, (
        "named for the JSON-RPC error channel but assert the in-band "
        f"`result.isError` instead: {offenders}"
    )


# How a test can legitimately claim the transport channel. Reading `["error"]`
# off an envelope, naming the exception type, or going through `call_error`,
# which asserts `"error" in envelope` on the caller's behalf.
TRANSPORT_FORMS = ('["error"]', "InvalidParamsError", "call_error(")


def test_a_transport_named_test_asserts_something_about_invalid_params():
    """The weaker companion: it must at least mention the channel or the error
    type, or the name is decoration."""
    offenders = []
    for path in _mcp_modules():
        for node in _test_functions(path):
            if not any(marker in node.name for marker in TRANSPORT_MARKERS):
                continue
            body = _source_of(path, node)
            if not any(form in body for form in TRANSPORT_FORMS):
                offenders.append(f"{path.name}::{node.name}")
    assert not offenders, f"named for a transport error but assert no form: {offenders}"


def test_the_accepted_forms_are_each_in_use():
    """A form nothing uses is a hole the guard cannot see through."""
    bodies = [
        _source_of(path, node)
        for path in _mcp_modules()
        for node in _test_functions(path)
        if any(marker in node.name for marker in TRANSPORT_MARKERS)
    ]
    unused = [form for form in TRANSPORT_FORMS if not any(form in body for body in bodies)]
    assert unused != list(TRANSPORT_FORMS), "no transport-named test asserts any form"


def test_an_in_band_named_test_asserts_the_result():
    offenders = []
    for path in _mcp_modules():
        for node in _test_functions(path):
            if not any(marker in node.name for marker in IN_BAND_MARKERS):
                continue
            if "isError" not in _source_of(path, node):
                offenders.append(f"{path.name}::{node.name}")
    assert not offenders, f"named for an in-band error but never assert `isError`: {offenders}"


def test_both_vocabularies_are_actually_in_use():
    """Either guard passes trivially if no test uses that naming."""
    names = [node.name for path in _mcp_modules() for node in _test_functions(path)]
    assert any(any(m in n for m in TRANSPORT_MARKERS) for n in names)
    assert any(any(m in n for m in IN_BAND_MARKERS) for n in names)


# ── and the distinction itself still holds ───────────────────────────


@pytest.mark.parametrize(
    ("arguments", "expect_in_band"),
    [
        ({"a": 1}, True),  # missing required argument -> tool execution error
        ({"a": 1, "b": "nope"}, True),  # bad type -> tool execution error
    ],
)
def test_an_argument_failure_is_reported_in_band(arguments, expect_in_band):
    """The contract the renamed tests assert, stated once here too."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    async def run():
        server = MCPServer(app)
        return await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "add", "arguments": arguments},
            },
            MCPSession(),
        )

    response = asyncio.run(run())
    assert ("result" in response) is expect_in_band
    assert response["result"]["isError"] is True


def test_an_unknown_tool_is_reported_on_the_error_channel():
    """The other side of the distinction: this one really is a transport error."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    async def run():
        server = MCPServer(app)
        return await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "nosuchtool", "arguments": {}},
            },
            MCPSession(),
        )

    response = asyncio.run(run())
    assert "error" in response
