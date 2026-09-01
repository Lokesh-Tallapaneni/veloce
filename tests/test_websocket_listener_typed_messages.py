"""Typed WebSocket messages — a listener callback's annotation validates each frame."""

from __future__ import annotations

from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Field

from veloce import Veloce, WebSocket
from veloce.testclient import TestClient

try:
    import msgspec as _msgspec
except ImportError:  # pragma: no cover - exercised in the no-msgspec CI leg
    _msgspec = None

requires_msgspec = pytest.mark.skipif(_msgspec is None, reason="msgspec is not installed")


# A real message set: the chat protocol the design doc measures ergonomics
# against. Three inbound messages, one shared tag field.
class Join(BaseModel):
    type: Literal["join"]
    room: str


class Say(BaseModel):
    type: Literal["say"]
    text: str


class Leave(BaseModel):
    type: Literal["leave"]


Inbound = Annotated[Join | Say | Leave, Field(discriminator="type")]


class Ambiguous1(BaseModel):
    x: int


class Ambiguous2(BaseModel):
    x: int


if _msgspec is not None:

    class MJoin(_msgspec.Struct, tag="join", tag_field="type"):
        room: str

    class MSay(_msgspec.Struct, tag="say", tag_field="type"):
        text: str

    class Untagged1(_msgspec.Struct):
        x: int

    class Untagged2(_msgspec.Struct):
        x: int


def test_tagged_union_dispatches_each_message_to_its_type():
    app = Veloce()
    seen: list[str] = []

    @app.websocket_listener("/chat")
    async def chat(message: Inbound):
        seen.append(type(message).__name__)
        if isinstance(message, Say):
            return {"said": message.text}
        if isinstance(message, Join):
            return {"joined": message.room}
        return {"left": True}

    with TestClient(app) as client, client.websocket_connect("/chat") as ws:
        ws.send_json({"type": "join", "room": "lobby"})
        assert ws.receive_json() == {"joined": "lobby"}
        ws.send_json({"type": "say", "text": "hi"})
        assert ws.receive_json() == {"said": "hi"}
        ws.send_json({"type": "leave"})
        assert ws.receive_json() == {"left": True}

    assert seen == ["Join", "Say", "Leave"]


def test_single_model_is_validated_and_passed_as_the_model():
    app = Veloce()

    @app.websocket_listener("/say")
    async def say(message: Say):
        assert isinstance(message, Say)
        return {"text": message.text}

    with TestClient(app) as client, client.websocket_connect("/say") as ws:
        ws.send_json({"type": "say", "text": "hello"})
        assert ws.receive_json() == {"text": "hello"}


def test_socket_first_signature_annotates_the_second_parameter():
    app = Veloce()

    @app.websocket_listener("/chat")
    async def chat(ws: WebSocket, message: Say):
        return {"text": message.text, "path": ws.scope["path"]}

    with TestClient(app) as client, client.websocket_connect("/chat") as ws:
        ws.send_json({"type": "say", "text": "hi"})
        assert ws.receive_json() == {"text": "hi", "path": "/chat"}


def test_frame_that_does_not_match_closes_1007_without_reaching_the_callback():
    app = Veloce()
    seen: list = []

    @app.websocket_listener("/say")
    async def say(message: Say):
        seen.append(message)
        return {"ok": True}

    with TestClient(app) as client, client.websocket_connect("/say") as ws:
        ws.send_json({"type": "say", "text": "fine"})
        assert ws.receive_json() == {"ok": True}
        # `text` is required, so this frame is not a `Say`.
        ws.send_json({"type": "say"})
        with pytest.raises(RuntimeError, match="1007"):
            ws.receive_json()
    assert len(seen) == 1


def test_unknown_tag_is_rejected_the_same_way():
    app = Veloce()

    @app.websocket_listener("/chat")
    async def chat(message: Inbound):
        return {"ok": True}

    with TestClient(app) as client, client.websocket_connect("/chat") as ws:
        ws.send_json({"type": "shout", "text": "hi"})
        with pytest.raises(RuntimeError, match="1007"):
            ws.receive_json()


def test_undiscriminated_pydantic_union_is_refused_at_registration():
    app = Veloce()

    with pytest.raises(TypeError, match="must be discriminated"):

        @app.websocket_listener("/amb")
        async def amb(message: Ambiguous1 | Ambiguous2):
            return None


def test_unannotated_callback_still_receives_the_raw_payload():
    app = Veloce()

    @app.websocket_listener("/echo")
    async def echo(data):
        return {"echo": data, "kind": type(data).__name__}

    with TestClient(app) as client, client.websocket_connect("/echo") as ws:
        ws.send_json({"anything": [1, 2]})
        assert ws.receive_json() == {"echo": {"anything": [1, 2]}, "kind": "dict"}
        ws.send_json([1, 2, 3])
        assert ws.receive_json() == {"echo": [1, 2, 3], "kind": "list"}


def test_text_mode_annotation_does_not_validate():
    """A `text` listener gets the frame as-is; `str` declares no model."""
    app = Veloce()

    @app.websocket_listener("/echo", receive="text", send="text")
    async def echo(data: str):
        return f"got:{data}"

    with TestClient(app) as client, client.websocket_connect("/echo") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "got:hi"


@requires_msgspec
def test_mixed_backend_union_is_refused_at_registration():
    app = Veloce()

    with pytest.raises(TypeError, match="one model backend"):

        @app.websocket_listener("/mix")
        async def mix(message: Say | MSay):
            return None


@requires_msgspec
def test_tagged_msgspec_union_dispatches():
    app = Veloce()

    @app.websocket_listener("/chat")
    async def chat(message: MJoin | MSay):
        if isinstance(message, MSay):
            return {"said": message.text}
        return {"joined": message.room}

    with TestClient(app) as client, client.websocket_connect("/chat") as ws:
        ws.send_json({"type": "join", "room": "lobby"})
        assert ws.receive_json() == {"joined": "lobby"}
        ws.send_json({"type": "say", "text": "hi"})
        assert ws.receive_json() == {"said": "hi"}


@requires_msgspec
def test_untagged_msgspec_union_is_refused_at_registration():
    app = Veloce()

    with pytest.raises(TypeError, match="must be discriminated"):

        @app.websocket_listener("/amb")
        async def amb(message: Untagged1 | Untagged2):
            return None


def test_unresolvable_annotation_warns_instead_of_silently_skipping():
    """A model the annotation cannot name is reported, not quietly ignored."""
    app = Veloce()

    class LocalOnly(BaseModel):
        x: int

    with pytest.warns(UserWarning, match="could not resolve the annotation"):

        @app.websocket_listener("/local")
        async def local(message: LocalOnly):
            return {"kind": type(message).__name__}

    with TestClient(app) as client, client.websocket_connect("/local") as ws:
        ws.send_json({"x": 1})
        assert ws.receive_json() == {"kind": "dict"}


def test_sync_callback_also_receives_the_validated_model():
    app = Veloce()

    @app.websocket_listener("/say")
    def say(message: Say):
        return {"text": message.text, "model": isinstance(message, Say)}

    with TestClient(app) as client, client.websocket_connect("/say") as ws:
        ws.send_json({"type": "say", "text": "hi"})
        assert ws.receive_json() == {"text": "hi", "model": True}


def test_on_disconnect_runs_after_a_rejected_frame_closes_the_socket():
    app = Veloce()
    torn_down: list[str] = []

    async def bye(ws: WebSocket):
        torn_down.append("bye")

    @app.websocket_listener("/say", on_disconnect=bye)
    async def say(message: Say):
        return {"ok": True}

    with TestClient(app) as client, client.websocket_connect("/say") as ws:
        ws.send_json({"type": "say"})
        with pytest.raises(RuntimeError, match="1007"):
            ws.receive_json()
    assert torn_down == ["bye"]


@requires_msgspec
def test_single_msgspec_struct_is_validated():
    app = Veloce()

    @app.websocket_listener("/say")
    async def say(message: MSay):
        return {"text": message.text, "model": isinstance(message, MSay)}

    with TestClient(app) as client, client.websocket_connect("/say") as ws:
        ws.send_json({"type": "say", "text": "hi"})
        assert ws.receive_json() == {"text": "hi", "model": True}
        ws.send_json({"type": "say"})
        with pytest.raises(RuntimeError, match="1007"):
            ws.receive_json()
