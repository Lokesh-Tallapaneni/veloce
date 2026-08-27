"""Every stdio reply is either written or answered with an error — never dropped.

The stdio writer called `orjson.dumps(payload)` with no `default=`, alone among
the server's writers. So a value the framework's own encoder handles — a
`Decimal`, a `set`, a `Path`, a type with a registered encoder — raised
`TypeError` inside a task nothing awaits.

The consequence was the worst failure shape available. Zero bytes written: no
reply, no JSON-RPC error, no log line. The same handler answered fine over HTTP
and hung forever over stdio, and `ctx.result_meta` is documented public API that
puts user values straight into the envelope.

The writer now uses the same fallback encoder as the HTTP path, so one handler
answering both doors produces the same JSON either way. And if a value survives
even that — a `Secret`, which the encoder refuses on purpose — the reply becomes
a JSON-RPC internal error carrying the request's id, logged, rather than silence.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

_HEADER = """
import asyncio
from decimal import Decimal
from pathlib import Path

from veloce import MCPContext, Veloce

app = Veloce(title="EncodeProbe", openapi_url=None)
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


def _run_server(tmp_path, tool_source: str) -> subprocess.CompletedProcess[str]:
    """Serve one `tools/call` from a real subprocess and return both streams."""
    script = tmp_path / "server.py"
    script.write_text(_HEADER + tool_source + _FOOTER, encoding="utf-8")
    env = dict(os.environ)
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


def _reply(result: subprocess.CompletedProcess[str], msg_id: int = 2) -> dict:
    """The reply to `msg_id`, failing loudly when nothing was written for it."""
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        message = json.loads(line)
        if message.get("id") == msg_id:
            return message
    pytest.fail(f"no reply for id {msg_id} on the wire; stderr:\n{result.stderr}")


def _meta_tool(expression: str) -> str:
    """A tool that puts `expression` into the reply envelope via `result_meta`."""
    return (
        '\n@app.mcp_tool(description="A tool")\n'
        "async def probe(ctx: MCPContext) -> dict:\n"
        f'    ctx.result_meta["io.example/value"] = {expression}\n'
        "    return {'ok': True}\n"
    )


# ── the values the framework's encoder handles ───────────────────────


@pytest.mark.parametrize(
    ("label", "expression"),
    [
        ("decimal", 'Decimal("0.25")'),
        ("set", "{1, 2, 3}"),
        ("path", 'Path("/tmp/x")'),
        ("frozenset", "frozenset({1})"),
        ("tuple of decimals", '(Decimal("1.5"),)'),
    ],
)
def test_a_reply_carrying_the_value_is_written(tmp_path, label, expression):
    """The defect: each of these wrote zero bytes and hung the client."""
    reply = _reply(_run_server(tmp_path, _meta_tool(expression)))
    assert "error" not in reply
    assert "io.example/value" in reply["result"]["_meta"]


def test_a_decimal_keeps_its_numeric_form(tmp_path):
    """Not a string, not a repr - the same shape the HTTP path emits."""
    reply = _reply(_run_server(tmp_path, _meta_tool('Decimal("0.25")')))
    assert reply["result"]["_meta"]["io.example/value"] == 0.25


def test_a_set_becomes_a_list(tmp_path):
    reply = _reply(_run_server(tmp_path, _meta_tool("{1, 2, 3}")))
    assert sorted(reply["result"]["_meta"]["io.example/value"]) == [1, 2, 3]


def test_a_registered_encoder_is_honoured(tmp_path):
    """The encoder registry is app-wide; stdio must consult it like HTTP does."""
    source = (
        "\nfrom veloce.encoders import register_encoder\n"
        "class Money:\n"
        "    def __init__(self, amount):\n        self.amount = amount\n"
        'register_encoder(Money, lambda m: {"amount": m.amount})\n'
        '\n@app.mcp_tool(description="A tool")\n'
        "async def probe(ctx: MCPContext) -> dict:\n"
        '    ctx.result_meta["io.example/value"] = Money(7)\n'
        "    return {'ok': True}\n"
    )
    reply = _reply(_run_server(tmp_path, source))
    assert reply["result"]["_meta"]["io.example/value"] == {"amount": 7}


def test_the_value_survives_in_a_nested_structure(tmp_path):
    reply = _reply(_run_server(tmp_path, _meta_tool('{"inner": [Decimal("1.5")]}')))
    assert reply["result"]["_meta"]["io.example/value"] == {"inner": [1.5]}


# ── a value nothing can encode still gets an answer ──────────────────


def test_a_refused_value_produces_an_error_not_silence(tmp_path):
    """A `Secret` is refused on purpose; the client must still hear back."""
    source = (
        "\nfrom veloce.secret import Secret\n"
        '\n@app.mcp_tool(description="A tool")\n'
        "async def probe(ctx: MCPContext) -> dict:\n"
        '    ctx.result_meta["io.example/value"] = Secret("hunter2")\n'
        "    return {'ok': True}\n"
    )
    reply = _reply(_run_server(tmp_path, source))
    assert reply["error"]["code"] == -32603


def test_the_error_carries_the_requests_id(tmp_path):
    """Without the id a client cannot settle the call it is waiting on."""
    source = (
        "\nfrom veloce.secret import Secret\n"
        '\n@app.mcp_tool(description="A tool")\n'
        "async def probe(ctx: MCPContext) -> dict:\n"
        '    ctx.result_meta["io.example/value"] = Secret("hunter2")\n'
        "    return {'ok': True}\n"
    )
    assert _reply(_run_server(tmp_path, source))["id"] == 2


def test_the_refused_value_is_not_leaked_into_the_error(tmp_path):
    """A `Secret` refused for being secret must not appear in the diagnostic."""
    source = (
        "\nfrom veloce.secret import Secret\n"
        '\n@app.mcp_tool(description="A tool")\n'
        "async def probe(ctx: MCPContext) -> dict:\n"
        '    ctx.result_meta["io.example/value"] = Secret("hunter2")\n'
        "    return {'ok': True}\n"
    )
    result = _run_server(tmp_path, source)
    assert "hunter2" not in result.stdout


def test_the_failure_is_logged(tmp_path):
    """Silence was the whole defect; something must reach stderr."""
    source = (
        "\nimport logging\n"
        "logging.basicConfig(level=logging.ERROR)\n"
        "from veloce.secret import Secret\n"
        '\n@app.mcp_tool(description="A tool")\n'
        "async def probe(ctx: MCPContext) -> dict:\n"
        '    ctx.result_meta["io.example/value"] = Secret("hunter2")\n'
        "    return {'ok': True}\n"
    )
    result = _run_server(tmp_path, source)
    assert "could not be encoded" in result.stderr


def test_the_server_keeps_serving_after_a_failed_reply(tmp_path):
    """One unencodable reply must not take the connection down."""
    source = (
        "\nfrom veloce.secret import Secret\n"
        '\n@app.mcp_tool(description="Fails to encode")\n'
        "async def probe(ctx: MCPContext) -> dict:\n"
        '    ctx.result_meta["io.example/value"] = Secret("hunter2")\n'
        "    return {'ok': True}\n"
        '\n@app.mcp_tool(description="Encodes fine")\n'
        "async def after() -> dict:\n"
        "    return {'still': 'here'}\n"
    )
    script = tmp_path / "server.py"
    script.write_text(_HEADER + source + _FOOTER, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    requests = [
        *_REQUESTS,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "after"}},
    ]
    result = subprocess.run(
        [sys.executable, str(script)],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert _reply(result, 2)["error"]["code"] == -32603
    assert "still" in json.dumps(_reply(result, 3))


# ── the ordinary path is unchanged ───────────────────────────────────


def test_a_plain_reply_still_goes_out(tmp_path):
    source = (
        '\n@app.mcp_tool(description="A tool")\n'
        "async def probe() -> dict:\n"
        "    return {'ok': True}\n"
    )
    reply = _reply(_run_server(tmp_path, source))
    assert "error" not in reply
    assert "ok" in json.dumps(reply)


def test_the_initialize_reply_still_goes_out(tmp_path):
    source = (
        '\n@app.mcp_tool(description="A tool")\n'
        "async def probe() -> dict:\n"
        "    return {'ok': True}\n"
    )
    reply = _reply(_run_server(tmp_path, source), msg_id=1)
    assert reply["result"]["protocolVersion"] == "2025-06-18"


def test_every_line_on_the_wire_is_json(tmp_path):
    """The error path writes to the wire too; it must not corrupt it."""
    source = (
        "\nfrom veloce.secret import Secret\n"
        '\n@app.mcp_tool(description="A tool")\n'
        "async def probe(ctx: MCPContext) -> dict:\n"
        '    ctx.result_meta["io.example/value"] = Secret("hunter2")\n'
        "    return {'ok': True}\n"
    )
    result = _run_server(tmp_path, source)
    for line in result.stdout.splitlines():
        if line.strip():
            json.loads(line)


# ── the writer is configured like the others ─────────────────────────


def test_the_stdio_writer_uses_the_shared_envelope_encoder():
    """A guard: it was the only writer with no fallback at all.

    It now shares `encode_envelope` with the HTTP and SSE transports, so all
    three frame the protocol identically.
    """
    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "veloce"
        / "contrib"
        / "mcp"
        / "transports"
        / "stdio.py"
    ).read_text(encoding="utf-8")
    assert "encode_envelope(payload)" in source
    assert "orjson.dumps(payload)" not in source
