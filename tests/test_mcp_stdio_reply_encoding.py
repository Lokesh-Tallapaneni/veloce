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

**One subprocess, not seventeen.** The subprocess is not incidental: this is
about what a real process writes to a real pipe, and an in-memory transport
would not have caught the defect. But a fresh interpreter per case cost
0.5 s each and 9.3 s for the module, the slowest in the suite. One server
registers every probe tool and answers one `tools/call` per case in order,
which is also a stronger arrangement than the original: seventeen isolated
processes could not show that a refused reply leaves the server serving, and
this run answers eight more calls after the refusal.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

# `logging.basicConfig` is in the header because one case asserts the encoder
# failure reaches stderr, and a handler has to exist for it to reach anything.
_HEADER = """
import asyncio
import logging
from decimal import Decimal
from pathlib import Path

from veloce import MCPContext, Veloce
from veloce.encoders import register_encoder
from veloce.secret import Secret

logging.basicConfig(level=logging.ERROR)

app = Veloce(title="EncodeProbe", openapi_url=None)


class Money:
    def __init__(self, amount):
        self.amount = amount


register_encoder(Money, lambda m: {"amount": m.amount})
"""

_FOOTER = "\nasyncio.run(app.mount_mcp())\n"


def _meta_tool(name: str, expression: str) -> str:
    """A tool that puts `expression` into the reply envelope via `result_meta`."""
    return (
        f'\n@app.mcp_tool(description="A tool")\n'
        f"async def {name}(ctx: MCPContext) -> dict:\n"
        f'    ctx.result_meta["io.example/value"] = {expression}\n'
        "    return {'ok': True}\n"
    )


# The values the framework's encoder handles. Each wrote zero bytes and hung
# the client before the fix.
_ENCODABLE = [
    ("decimal", 'Decimal("0.25")'),
    ("set", "{1, 2, 3}"),
    ("path", 'Path("/tmp/x")'),
    ("frozenset", "frozenset({1})"),
    ("tuple_of_decimals", '(Decimal("1.5"),)'),
    ("registered_encoder", "Money(7)"),
    ("nested", '{"inner": [Decimal("1.5")]}'),
]

# Order matters: the refusal is answered before `after`, so the run shows the
# server still serving past a reply it could not encode.
_TOOLS = (
    "".join(_meta_tool(f"probe_{name}", expression) for name, expression in _ENCODABLE)
    + _meta_tool("probe_secret", 'Secret("hunter2")')
    + (
        '\n@app.mcp_tool(description="A plain tool")\n'
        "async def probe_plain() -> dict:\n"
        "    return {'ok': True}\n"
        '\n@app.mcp_tool(description="Encodes fine")\n'
        "async def after() -> dict:\n"
        "    return {'still': 'here'}\n"
    )
)

# id 1 is `initialize`; each tool's call takes the next id in this order.
_CALL_ORDER = [f"probe_{name}" for name, _ in _ENCODABLE] + [
    "probe_secret",
    "probe_plain",
    "after",
]
CALL_ID = {name: index + 2 for index, name in enumerate(_CALL_ORDER)}


class Wire:
    """What one server process wrote, indexed by JSON-RPC id."""

    def __init__(self, stdout: str, stderr: str) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.replies: dict[int, dict] = {}
        for line in stdout.splitlines():
            if line.strip():
                message = json.loads(line)
                if "id" in message:
                    self.replies[message["id"]] = message

    def reply(self, name_or_id: str | int) -> dict:
        """The reply to a tool call, failing loudly when nothing was written."""
        msg_id = name_or_id if isinstance(name_or_id, int) else CALL_ID[name_or_id]
        if msg_id not in self.replies:
            pytest.fail(f"no reply for id {msg_id} on the wire; stderr:\n{self.stderr}")
        return self.replies[msg_id]

    def meta(self, name: str) -> object:
        return self.reply(name)["result"]["_meta"]["io.example/value"]


@pytest.fixture(scope="session")
def wire(tmp_path_factory) -> Wire:
    """One real server process, answering one call per case."""
    script = tmp_path_factory.mktemp("stdio") / "server.py"
    script.write_text(_HEADER + _TOOLS + _FOOTER, encoding="utf-8")
    requests = [
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
        *(
            {
                "jsonrpc": "2.0",
                "id": CALL_ID[name],
                "method": "tools/call",
                "params": {"name": name},
            }
            for name in _CALL_ORDER
        ),
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    result = subprocess.run(
        [sys.executable, str(script)],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    return Wire(result.stdout, result.stderr)


# ── the values the framework's encoder handles ───────────────────────


@pytest.mark.parametrize("name", [name for name, _ in _ENCODABLE])
def test_a_reply_carrying_the_value_is_written(wire, name):
    """The defect: each of these wrote zero bytes and hung the client."""
    reply = wire.reply(f"probe_{name}")
    assert "error" not in reply
    assert "io.example/value" in reply["result"]["_meta"]


def test_a_decimal_keeps_its_numeric_form(wire):
    """Not a string, not a repr - the same shape the HTTP path emits."""
    assert wire.meta("probe_decimal") == 0.25


def test_a_set_becomes_a_list(wire):
    assert sorted(wire.meta("probe_set")) == [1, 2, 3]


def test_a_registered_encoder_is_honoured(wire):
    """The encoder registry is app-wide; stdio must consult it like HTTP does."""
    assert wire.meta("probe_registered_encoder") == {"amount": 7}


def test_the_value_survives_in_a_nested_structure(wire):
    assert wire.meta("probe_nested") == {"inner": [1.5]}


# ── a value nothing can encode still gets an answer ──────────────────


def test_a_refused_value_produces_an_error_not_silence(wire):
    """A `Secret` is refused on purpose; the client must still hear back."""
    assert wire.reply("probe_secret")["error"]["code"] == -32603


def test_the_error_carries_the_requests_id(wire):
    """Without the id a client cannot settle the call it is waiting on."""
    assert wire.reply("probe_secret")["id"] == CALL_ID["probe_secret"]


def test_the_refused_value_is_not_leaked_into_the_error(wire):
    """A `Secret` refused for being secret must not appear in the diagnostic."""
    assert "hunter2" not in wire.stdout


def test_the_failure_is_logged(wire):
    """Silence was the whole defect; something must reach stderr."""
    assert "could not be encoded" in wire.stderr


def test_the_server_keeps_serving_after_a_failed_reply(wire):
    """One unencodable reply must not take the connection down.

    `after` is called two ids past the refusal, so this is the same run rather
    than a second process arranged to look like one.
    """
    assert wire.reply("probe_secret")["error"]["code"] == -32603
    assert "still" in json.dumps(wire.reply("after"))


# ── the ordinary path is unchanged ───────────────────────────────────


def test_a_plain_reply_still_goes_out(wire):
    reply = wire.reply("probe_plain")
    assert "error" not in reply
    assert "ok" in json.dumps(reply)


def test_the_initialize_reply_still_goes_out(wire):
    assert wire.reply(1)["result"]["protocolVersion"] == "2025-06-18"


def test_every_line_on_the_wire_is_json(wire):
    """The error path writes to the wire too; it must not corrupt it."""
    for line in wire.stdout.splitlines():
        if line.strip():
            json.loads(line)


def test_every_call_was_answered(wire):
    """The harness is the guard: a reply that never arrived must not read as a pass.

    Every assertion above indexes `wire.replies`, so a server that died after
    the first call would fail loudly here rather than in whichever test
    happened to run first.
    """
    assert set(wire.replies) == {1, *CALL_ID.values()}


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
