"""The websocket message contract — one record, shared by the loop and any lowering."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from tests._routes import route_at
from veloce import Veloce
from veloce._model_backend import ModelBackend
from veloce._route_contract import RouteContract
from veloce._ws_listener import WSMessageContract, build_listener_handler
from veloce.testclient import TestClient


class Join(BaseModel):
    type: Literal["join"]
    room: str


class Say(BaseModel):
    type: Literal["say"]
    text: str


Inbound = Annotated[Join | Say, Field(discriminator="type")]


def test_a_union_listener_reports_its_members_and_discriminator():
    async def chat(message: Inbound):
        return None

    _handler, contract = build_listener_handler(chat)

    assert isinstance(contract, WSMessageContract)
    assert contract.message_type is Inbound
    assert contract.members == (Join, Say)
    assert contract.discriminator == "type"
    assert contract.backend is ModelBackend.PYDANTIC


def test_a_single_model_listener_reports_no_discriminator():
    async def say(message: Say):
        return None

    _handler, contract = build_listener_handler(say)

    assert contract.message_type is Say
    assert contract.members == (Say,)
    assert contract.discriminator is None


def test_an_untyped_listener_builds_no_contract():
    async def echo(data):
        return data

    _handler, contract = build_listener_handler(echo)

    assert contract is None


def test_a_non_model_annotation_builds_no_contract():
    """`data: dict` names no model, so the frame stays raw."""

    async def echo(data: dict):
        return data

    _handler, contract = build_listener_handler(echo)

    assert contract is None


def test_the_route_holds_the_same_contract_the_loop_validates_through():
    """Not a copy: a lowering reads the object the receive loop closed over."""
    app = Veloce()

    @app.websocket_listener("/chat")
    async def chat(message: Inbound):
        return None

    info = route_at(app, "/chat", include_hidden=True)
    contract = info.ws_messages

    assert isinstance(contract, WSMessageContract)
    closed_over = [cell.cell_contents for cell in info.handler.__closure__ or ()]
    assert any(value is contract.validate for value in closed_over), (
        "the loop validates through a different callable than the route publishes"
    )


def test_routes_without_a_typed_message_carry_none():
    app = Veloce()

    @app.websocket_listener("/echo")
    async def echo(data):
        return data

    @app.websocket_listener("/text", receive="text", send="text")
    async def text(data: str):
        return data

    @app.websocket("/raw")
    async def raw(ws):
        await ws.accept()

    @app.get("/http")
    async def http():
        return {}

    for path in ("/echo", "/text", "/raw"):
        assert route_at(app, path, include_hidden=True).ws_messages is None
    assert route_at(app, "/http").ws_messages is None


def test_the_typed_listener_still_serves_traffic():
    app = Veloce()

    @app.websocket_listener("/chat")
    async def chat(message: Inbound):
        return {"kind": type(message).__name__}

    with TestClient(app) as client, client.websocket_connect("/chat") as ws:
        ws.send_json({"type": "say", "text": "hi"})
        assert ws.receive_json() == {"kind": "Say"}


def test_the_route_contract_projects_the_message_contract():
    app = Veloce()

    @app.websocket_listener("/chat")
    async def chat(message: Inbound):
        return None

    info = route_at(app, "/chat", include_hidden=True)
    projected = RouteContract.from_route_info(info)

    assert projected.ws_messages is info.ws_messages
    assert projected.ws_messages.message_type is Inbound


def test_the_route_contract_projects_none_for_an_http_route():
    app = Veloce()

    @app.get("/items")
    async def items():
        return []

    info = route_at(app, "/items")
    assert RouteContract.from_route_info(info).ws_messages is None


class Ack(BaseModel):
    ok: bool


class Broadcast(BaseModel):
    text: str


def test_the_return_annotation_supplies_the_send_contract():
    async def chat(message: Inbound) -> Ack:
        return Ack(ok=True)

    _handler, contract = build_listener_handler(chat)
    assert contract.send_type is Ack


def test_a_send_union_is_recorded_whole_and_undiscriminated():
    async def chat(message: Inbound) -> Ack | Broadcast | None:
        return None

    _handler, contract = build_listener_handler(chat)
    assert contract.send_type == Ack | Broadcast | None


def test_no_return_annotation_records_no_send_contract():
    async def chat(message: Inbound):
        return None

    _handler, contract = build_listener_handler(chat)
    assert contract.send_type is None


def test_a_send_contract_does_not_filter_the_sent_value():
    """Recording the type must not start shaping the frame."""
    app = Veloce()

    @app.websocket_listener("/chat")
    async def chat(message: Inbound) -> Ack:
        return {"ok": True, "extra": "kept"}

    with TestClient(app) as client, client.websocket_connect("/chat") as ws:
        ws.send_json({"type": "say", "text": "hi"})
        assert ws.receive_json() == {"ok": True, "extra": "kept"}
