"""The websocket message contract — one record, shared by the loop and any lowering."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from veloce._model_backend import ModelBackend
from veloce._ws_listener import WSMessageContract, build_listener_handler


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
