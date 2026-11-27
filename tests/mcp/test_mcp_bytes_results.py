"""Raw bytes in a tool result.

A tool result carries text, so bytes have to become a string. Rendering them
with `str()` produced the Python repr - `b'...'` quoting and all - which is
neither the value the caller returned nor recoverable. Bytes that are UTF-8 are
now decoded; bytes that are not are base64-encoded rather than decoded with
replacement, which would corrupt them beyond recovery.
"""

from __future__ import annotations

import base64
import json

from veloce import Veloce
from veloce.contrib.mcp._helpers import _stringify
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


# ── The shaping helper ───────────────────────────────────────────────


def test_utf8_bytes_decode_to_their_text():
    """A bare byte string is the result text, so it is handed over unquoted."""
    assert _stringify(b"raw-bytes-value") == "raw-bytes-value"


def test_the_python_repr_never_leaks():
    """The bug: `b'raw-bytes-value'`, quoting included, reached the client."""
    assert "b'" not in _stringify(b"raw-bytes-value")


def test_non_ascii_utf8_survives_a_round_trip():
    assert _stringify("café".encode()) == "café"


def test_json_shaped_bytes_are_not_double_encoded():
    assert _stringify(b'{"a":1}') == '{"a":1}'


def test_binary_bytes_are_base64_not_mangled():
    assert base64.b64decode(_stringify(PNG_HEADER)) == PNG_HEADER


def test_invalid_utf8_is_recoverable():
    raw = b"\xff\xfe\x00bad"
    assert base64.b64decode(_stringify(raw)) == raw


def test_empty_bytes_render_as_an_empty_string():
    assert _stringify(b"") == ""


def test_a_bytearray_is_shaped_like_bytes():
    assert _stringify(bytearray(b"hello")) == "hello"


def test_a_memoryview_is_shaped_like_bytes():
    assert _stringify(memoryview(b"hello")) == "hello"


def test_bytes_nested_in_a_mapping_are_shaped_too():
    assert json.loads(_stringify({"data": b"nested"})) == {"data": "nested"}


def test_bytes_nested_in_a_sequence_are_shaped_too():
    assert json.loads(_stringify([b"a", PNG_HEADER])) == [
        "a",
        base64.b64encode(PNG_HEADER).decode("ascii"),
    ]


def test_a_string_return_is_untouched():
    assert _stringify("already text") == "already text"


def test_a_mapping_return_is_still_json():
    assert json.loads(_stringify({"a": 1})) == {"a": 1}


# ── Through a real tool call ─────────────────────────────────────────


def _server(*tools) -> MCPServer:
    app = Veloce(title="BytesProbe", version="1.0.0", openapi_url=None)
    for fn in tools:
        app.mcp_tool(description=f"tool {fn.__name__}")(fn)
    return MCPServer(app)


async def _text(server: MCPServer, name: str) -> str:
    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        },
        MCPSession(),
    )
    result = response["result"]
    assert not result.get("isError"), result["content"][0]["text"]
    return result["content"][0]["text"]


async def test_a_tool_returning_text_bytes_reads_as_text():
    async def read_note() -> bytes:
        return b"the note body"

    assert await _text(_server(read_note), "read_note") == "the note body"


async def test_a_tool_returning_binary_bytes_stays_recoverable():
    async def read_image() -> bytes:
        return PNG_HEADER

    assert base64.b64decode(await _text(_server(read_image), "read_image")) == PNG_HEADER


async def test_a_tool_returning_bytes_inside_a_payload():
    async def read_payload() -> dict:
        return {"name": "n", "blob": b"inner"}

    assert json.loads(await _text(_server(read_payload), "read_payload")) == {
        "name": "n",
        "blob": "inner",
    }
