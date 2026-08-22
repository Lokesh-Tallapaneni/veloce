"""One handler, two doors — and one JSON encoding.

A route exposed as an MCP tool is the same handler either way, so the value it
returns has to reach an agent and a browser as the same JSON. It did not: the
MCP result text went through a five-line fallback that stringified anything it
did not recognise, so a `set` arrived as a Python repr, a `Decimal` lost its
numeric form, a registered encoder was ignored, and a `Secret` - which the HTTP
door refuses outright - was emitted.

Both doors now share `orjson_default`. Bytes are the single deliberate
difference and are pinned here so it stays deliberate.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import decimal
import pathlib
from collections import deque

import orjson
import pytest
from pydantic import BaseModel, Field, computed_field

from veloce import Secret, TestClient, Veloce, register_encoder, unregister_encoder
from veloce.contrib.mcp._helpers import _stringify
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.encoders import jsonable_encoder, orjson_default


def _http(value: object) -> str:
    """What the HTTP door writes for this value."""
    return orjson.dumps(value, default=orjson_default).decode()


def _mcp(value: object) -> str:
    """What the MCP door writes for the same value."""
    return _stringify(value)


def _both(value: object) -> tuple[str, str]:
    return _http(value), _mcp(value)


# ── The doors agree ──────────────────────────────────────────────────


def test_a_set_is_a_list_through_both_doors():
    """It arrived as `"{'b', 'a'}"` - a Python repr inside a JSON string."""
    assert _both({"tags": {"b", "a"}}) == ('{"tags":["a","b"]}',) * 2


def test_a_frozenset_is_a_list_through_both_doors():
    assert _both({"tags": frozenset({"x"})}) == ('{"tags":["x"]}',) * 2


def test_a_deque_is_a_list_through_both_doors():
    assert _both({"q": deque([1, 2])}) == ('{"q":[1,2]}',) * 2


def test_an_integer_valued_decimal_stays_an_integer():
    """Not `"1"`: the numeric form is why `_decimal_to_json` exists."""
    assert _both({"n": decimal.Decimal("1")}) == ('{"n":1}',) * 2


def test_a_fractional_decimal_stays_a_number():
    assert _both({"n": decimal.Decimal("1.50")}) == ('{"n":1.5}',) * 2


def test_a_timedelta_is_its_seconds_through_both_doors():
    delta = datetime.timedelta(minutes=1, seconds=30)
    assert _both({"d": delta}) == ('{"d":90.0}',) * 2


def test_a_path_is_a_string_through_both_doors():
    http, mcp = _both({"p": pathlib.Path("a") / "b"})
    assert http == mcp


def test_an_unknown_object_publishes_its_public_attributes():
    class Money:
        def __init__(self) -> None:
            self.amount = 3
            self._internal = "hidden"

    http, mcp = _both({"o": Money()})
    assert http == mcp == '{"o":{"amount":3}}'


def test_an_object_with_no_attributes_falls_back_to_its_text():
    http, mcp = _both({"o": object()})
    assert http == mcp
    assert "object object at" in http


# ── Models ───────────────────────────────────────────────────────────


class Report(BaseModel):
    plain: int = 1
    aliased: str = Field(default="x", serialization_alias="renamed")
    when: datetime.date = datetime.date(2026, 1, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def doubled(self) -> int:
        return self.plain * 2


def test_a_model_encodes_the_same_through_both_doors():
    http, mcp = _both({"m": Report()})
    assert http == mcp


def test_a_computed_field_survives_both_doors():
    """`vars()` cannot see one, so the HTTP fallback used to drop it."""
    assert "doubled" in _http({"m": Report()})
    assert "doubled" in _mcp({"m": Report()})


def test_the_fallback_hook_agrees_with_the_documented_encoder():
    """`orjson_default`'s docstring promises the two paths agree; they now do."""
    through_hook = orjson.loads(_http(Report()))
    through_encoder = jsonable_encoder(Report())
    assert through_hook == through_encoder


def test_a_nested_model_encodes_the_same_through_both_doors():
    class Wrapper(BaseModel):
        inner: Report = Report()

    http, mcp = _both({"w": Wrapper()})
    assert http == mcp
    assert "doubled" in http


# ── A registered encoder is honoured by both ─────────────────────────


class Money:
    def __init__(self, amount: int, currency: str) -> None:
        self.amount = amount
        self.currency = currency


@pytest.fixture
def registered_money():
    register_encoder(Money, lambda m: {"amount": m.amount, "currency": m.currency})
    yield
    unregister_encoder(Money)


def test_a_registered_encoder_reaches_both_doors(registered_money):
    """The MCP door ignored the registry entirely."""
    http, mcp = _both({"o": Money(3, "USD")})
    assert http == mcp == '{"o":{"amount":3,"currency":"USD"}}'


# ── A secret is refused on both doors ────────────────────────────────


def test_a_secret_is_refused_on_the_http_door():
    with pytest.raises(TypeError) as raised:
        _http({"token": Secret("s3cret")})
    assert "must not be serialized" in str(raised.value.__cause__)


def test_a_secret_is_refused_on_the_mcp_door():
    """It used to be emitted as its masked text, which is still an emission."""
    with pytest.raises(TypeError) as raised:
        _mcp({"token": Secret("s3cret")})
    assert "must not be serialized" in str(raised.value.__cause__)


async def test_a_tool_returning_a_secret_reports_an_error_not_a_repr():
    app = Veloce(title="Leaky", openapi_url=None)

    @app.mcp_tool(description="Hands back a secret by mistake")
    async def leak() -> dict:
        return {"token": Secret("s3cret")}

    response = await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "leak", "arguments": {}},
        },
        MCPSession(),
    )
    # A refusal, not a result: it used to answer `{"token": "***"}`, which is a
    # masked placeholder reaching the model as though the tool had succeeded.
    assert "result" not in response
    assert response["error"]["code"] == -32603
    rendered = orjson.dumps(response).decode()
    assert "s3cret" not in rendered
    assert "{'token'" not in rendered


# ── Bytes: the one deliberate difference ─────────────────────────────


def test_bytes_are_the_documented_exception():
    """A tool result carries text, so a tool's bytes are decoded, not base64."""
    assert _http({"b": b"hi"}) == '{"b":"aGk="}'
    assert _mcp({"b": b"hi"}) == '{"b":"hi"}'


def test_binary_bytes_are_base64_on_both_doors():
    """Not decodable as text, so both doors fall back to the lossless form."""
    png = b"\x89PNG\r\n\x1a\n"
    assert orjson.loads(_http({"b": png}))["b"] == base64.b64encode(png).decode()
    assert orjson.loads(_mcp({"b": png}))["b"] == base64.b64encode(png).decode()


# ── End to end: one route, both doors ────────────────────────────────


def _two_door_app() -> Veloce:
    app = Veloce(title="Reports", version="1.0.0", openapi_url=None)

    @app.get(
        "/report",
        expose_as_mcp_tool=True,
        mcp_description="Return the current report",
    )
    async def report() -> dict:
        return {
            "tags": {"eu"},
            "price": decimal.Decimal("19.99"),
            "ttl": datetime.timedelta(minutes=5),
            "queue": deque([1, 2]),
        }

    return app


def test_one_route_answers_both_doors_identically():
    app = _two_door_app()

    with TestClient(app) as client:
        over_http = client.get("/report").text

    response = asyncio.run(
        MCPServer(app).handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "report", "arguments": {}},
            },
            MCPSession(),
        )
    )
    over_mcp = response["result"]["content"][0]["text"]
    assert orjson.loads(over_http) == orjson.loads(over_mcp)


# ── The other supported model backend ────────────────────────────────


def test_a_msgspec_struct_publishes_its_fields_on_both_doors():
    """`vars()` on a slotted Struct raises, so it used to arrive as a repr."""
    msgspec = pytest.importorskip("msgspec")

    class Point(msgspec.Struct):
        x: int = 1
        y: str = "up"

    http, mcp = _both({"s": Point()})
    assert http == mcp == '{"s":{"x":1,"y":"up"}}'


def test_a_nested_value_inside_a_struct_is_converted_too():
    msgspec = pytest.importorskip("msgspec")

    class Priced(msgspec.Struct):
        cost: decimal.Decimal = decimal.Decimal("2.50")

    http, mcp = _both({"s": Priced()})
    assert http == mcp == '{"s":{"cost":2.5}}'
