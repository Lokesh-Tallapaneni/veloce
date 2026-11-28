"""The message contract survives router and blueprint merges."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from tests._routes import route_at
from veloce import Blueprint, Router, Veloce
from veloce._ws_listener import WSMessageContract


class Say(BaseModel):
    type: Literal["say"]
    text: str


class Join(BaseModel):
    type: Literal["join"]
    room: str


Inbound = Annotated[Join | Say, Field(discriminator="type")]


def test_include_router_carries_the_contract():
    child = Router()

    @child.websocket_listener("/chat")
    async def chat(message: Inbound):
        return None

    source = next(
        info for _m, path, info in child.iter_routes(include_hidden=True) if path == "/chat"
    ).ws_messages

    app = Veloce()
    app.include_router(child, prefix="/api")

    merged = route_at(app, "/api/chat", include_hidden=True).ws_messages
    assert isinstance(merged, WSMessageContract)
    assert merged is source


def test_blueprint_registration_carries_the_contract():
    bp = Blueprint("chat")

    @bp.websocket_listener("/chat")
    async def chat(message: Inbound):
        return None

    app = Veloce()
    app.register_blueprint(bp, url_prefix="/bp")

    merged = route_at(app, "/bp/chat", include_hidden=True).ws_messages
    assert isinstance(merged, WSMessageContract)
    assert merged.members == (Join, Say)


def test_including_the_same_router_twice_is_still_idempotent():
    """A re-mount carries one contract object, so route identity still matches."""
    child = Router()

    @child.websocket_listener("/chat")
    async def chat(message: Inbound):
        return None

    app = Veloce()
    app.include_router(child, prefix="/api")
    app.include_router(child, prefix="/api")

    assert route_at(app, "/api/chat", include_hidden=True).ws_messages is not None
