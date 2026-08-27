"""The stdio protocol wire is isolated from everything else the process writes.

While a stdio server runs, the process's standard output *is* the protocol pipe,
so a `print` left in a handler, a library logging to stdout, or a subprocess a
tool spawns lands in the newline-delimited JSON stream as a line the client
cannot parse. The client reports malformed JSON, which points at everything
except the write that caused it.

The isolation is descriptor-level because a child inherits descriptors, not
Python file objects: rebinding `sys.stdout` would leave a shelling-out tool free
to corrupt the wire. These tests therefore run a real server in a real
subprocess and read its two streams apart.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.transports import stdio
from veloce.contrib.mcp.transports.stdio import _isolated_wire

_HEADER = """
import asyncio
import subprocess
import sys

from veloce import Veloce

app = Veloce(title="WireProbe", openapi_url=None)
"""

_FOOTER = "\nasyncio.run(app.mount_mcp())\n"

_REQUESTS = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "probe"}},
]


def _run_server(tmp_path, handler_body: str) -> subprocess.CompletedProcess[str]:
    """Serve one `tools/call` from a real subprocess and return both streams."""
    script = tmp_path / "server.py"
    script.write_text(
        _HEADER
        + '\n@app.mcp_tool(description="A tool")\nasync def probe() -> dict:\n'
        + handler_body
        + "    return {'ok': True}\n"
        + _FOOTER,
        encoding="utf-8",
    )
    env = dict(os.environ)
    # The child must import the same veloce the test did, wherever that is.
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return subprocess.run(
        [sys.executable, str(script)],
        input="".join(json.dumps(request) + "\n" for request in _REQUESTS),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def _wire_messages(stdout: str) -> list[dict]:
    """Every line the client would read off the wire, parsed.

    A line that is not JSON is the defect this module exists for, so it is
    surfaced as a failure here rather than silently skipped.
    """
    messages = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:  # pragma: no cover - only on a regression
            pytest.fail(f"non-JSON line on the protocol wire: {line!r}")
    return messages


# ── Nothing but the protocol reaches the wire ────────────────────────


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("print", "    print('polluting the wire')\n"),
        ("direct write", "    sys.stdout.write('polluting\\n')\n    sys.stdout.flush()\n"),
        (
            "child process",
            "    subprocess.run([sys.executable, '-c', \"print('from a child')\"], check=False)\n",
        ),
    ],
)
def test_handler_output_never_reaches_the_wire(tmp_path, label, body):
    result = _run_server(tmp_path, body)
    messages = _wire_messages(result.stdout)
    assert [m.get("id") for m in messages] == [1, 2]
    assert "result" in messages[1]


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("    print('a stray line')\n", "a stray line"),
        (
            "    subprocess.run([sys.executable, '-c', \"print('from a child')\"], check=False)\n",
            "from a child",
        ),
    ],
)
def test_diverted_output_appears_on_stderr(tmp_path, body, needle):
    result = _run_server(tmp_path, body)
    assert needle in result.stderr
    assert needle not in result.stdout


def test_a_lot_of_handler_output_still_leaves_the_wire_clean(tmp_path):
    """Volume is what defeats a buffer-sized workaround."""
    result = _run_server(tmp_path, "    for i in range(500):\n        print('noise', i)\n")
    messages = _wire_messages(result.stdout)
    assert [m.get("id") for m in messages] == [1, 2]


# ── The wire's own framing is unchanged ──────────────────────────────


def test_the_wire_is_newline_delimited_json_and_nothing_else(tmp_path):
    result = _run_server(tmp_path, "    print('noise')\n")
    assert "\r" not in result.stdout  # the transport frames LF, never CRLF
    assert result.stdout.count("\n") == len(_wire_messages(result.stdout))


def test_the_call_result_still_arrives(tmp_path):
    """Guard the obvious regression: isolating the wire must not silence it."""
    result = _run_server(tmp_path, "    print('noise')\n")
    reply = _wire_messages(result.stdout)[1]
    assert json.loads(reply["result"]["content"][0]["text"]) == {"ok": True}


# ── Stdin is isolated too ────────────────────────────────────────────


def test_a_handler_reading_stdin_cannot_steal_the_next_request(tmp_path):
    """Descriptor 0 is the protocol pipe; a handler reading it eats a message.

    It now reads the null device instead, so it sees EOF and the request that
    followed still reaches the server.
    """
    script_body = "    sys.stderr.write('stdin=%r\\n' % sys.stdin.readline())\n"
    result = _run_server(tmp_path, script_body)
    assert "stdin=''" in result.stderr
    assert [m.get("id") for m in _wire_messages(result.stdout)] == [1, 2]


# ── The process is left as it was found ──────────────────────────────


async def test_the_standard_descriptors_are_restored_after_serving():
    """A library that diverts descriptors and does not put them back is a trap."""

    before = (os.fstat(0), os.fstat(1))
    with _isolated_wire() as (reader, writer):
        assert reader is not None and writer is not None
    after = (os.fstat(0), os.fstat(1))
    assert [(s.st_dev, s.st_ino) for s in before] == [(s.st_dev, s.st_ino) for s in after]


async def test_a_second_server_is_refused_rather_than_left_to_contend():
    """Two servers would each divert the other's descriptors."""

    app = Veloce(title="Twice", openapi_url=None)

    @app.mcp_tool(description="A tool")
    async def a_tool() -> dict:
        return {"ok": True}

    stdio._wire_claimed = True
    try:
        with pytest.raises(RuntimeError, match="already serving"):
            await stdio.serve_stdio(MCPServer(app))
    finally:
        stdio._wire_claimed = False


async def test_the_claim_is_released_even_when_serving_raises():
    """A failed run must not leave the process unable to serve again."""

    class _Boom(Exception):
        pass

    async def _explode(*args: object, **kwargs: object) -> None:
        raise _Boom

    original = stdio._serve_on
    stdio._serve_on = _explode  # type: ignore[assignment]
    try:
        with pytest.raises(_Boom):
            await stdio.serve_stdio(None)  # type: ignore[arg-type]
    finally:
        stdio._serve_on = original  # type: ignore[assignment]
    assert stdio._wire_claimed is False
