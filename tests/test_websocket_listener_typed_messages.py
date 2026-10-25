"""Typed WebSocket messages — a listener callback's annotation validates each frame."""

from __future__ import annotations

from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Field

from tests._routes import route_at
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

    contract = route_at(app, "/chat", include_hidden=True).ws_messages
    assert contract.discriminator == "type"


@requires_msgspec
def test_untagged_msgspec_union_is_refused_at_registration():
    app = Veloce()

    with pytest.raises(TypeError, match="must be discriminated"):

        @app.websocket_listener("/amb")
        async def amb(message: Untagged1 | Untagged2):
            return None


def test_unresolvable_annotation_is_refused_instead_of_silently_skipping():
    """A model the annotation cannot name is refused, not quietly ignored.

    This asserted the warning until the fail-open shape was closed. Warning was
    an improvement on silence, but not a control: the listener still went on to
    accept every frame unvalidated.
    """
    app = Veloce()

    class LocalOnly(BaseModel):
        x: int

    with pytest.raises(TypeError, match="could not resolve the annotation"):

        @app.websocket_listener("/local")
        async def local(message: LocalOnly):
            return {"kind": type(message).__name__}


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


# ── a folded `Field` on a scalar message ────────────────────────────


def test_a_scalar_messages_field_bound_is_enforced():
    """`Annotated[int, Field(ge=1)]` declares a bound on this door too.

    The model spelling already closed 1007; the scalar one had no validator at
    all, so the frame arrived unchecked and the handler received a value its
    own declaration forbids.
    """
    app = Veloce()
    seen: list = []

    @app.websocket_listener("/n")
    async def numbers(value: Annotated[int, Field(ge=1)]):
        seen.append(value)
        return {"value": value}

    with TestClient(app) as client, client.websocket_connect("/n") as ws:
        ws.send_json(5)
        assert ws.receive_json() == {"value": 5}
        ws.send_json(0)
        with pytest.raises(RuntimeError, match="1007"):
            ws.receive_json()
    assert seen == [5]


def test_a_bound_under_an_outer_optional_is_still_enforced():
    """`Optional[Annotated[int, Field(ge=1)]]` declares the same bound.

    The frame door read `__metadata__` straight off the annotation, which sees
    past the outer `Optional` and found nothing, so the bound was enforced on a
    query parameter of this spelling and ignored on a frame of it.
    """
    app = Veloce()
    seen: list = []

    @app.websocket_listener("/outer")
    async def outer(value: Annotated[int, Field(ge=1)] | None = None):
        seen.append(value)
        return {"value": value}

    with TestClient(app) as client, client.websocket_connect("/outer") as ws:
        ws.send_json(5)
        assert ws.receive_json() == {"value": 5}
        ws.send_json(0)
        with pytest.raises(RuntimeError, match="1007"):
            ws.receive_json()
    assert seen == [5]


def test_an_unbounded_scalar_message_is_unchanged(recwarn):
    """The negative: a scalar with no bound keeps passing through untouched."""
    app = Veloce()
    seen: list = []

    @app.websocket_listener("/raw")
    async def raw(value: int):
        seen.append(value)
        return {"value": value}

    with TestClient(app) as client, client.websocket_connect("/raw") as ws:
        ws.send_json(0)
        assert ws.receive_json() == {"value": 0}
    assert seen == [0]


def test_a_nullable_scalar_message_still_accepts_null():
    """`Optional[int]` with a `Field` asked for `null`, so it must keep getting
    it - the first version of this guard detected nullability from a tuple that
    is always empty on this branch, so it refused every null frame."""
    app = Veloce()
    seen: list = []

    @app.websocket_listener("/maybe")
    async def maybe(value: Annotated[int | None, Field(ge=1)]):
        seen.append(value)
        return {"value": value}

    with TestClient(app) as client, client.websocket_connect("/maybe") as ws:
        ws.send_json(None)
        assert ws.receive_json() == {"value": None}
        ws.send_json(5)
        assert ws.receive_json() == {"value": 5}
    assert seen == [None, 5]


@pytest.mark.parametrize(
    "frame", ["0", "abc", [1, 2, 3], {"a": 1}, True], ids=["str", "text", "list", "object", "bool"]
)
def test_a_wrong_json_type_cannot_bypass_a_declared_bound(frame):
    """`ParamBase.validate` dispatches on the runtime value, so a frame of the
    wrong JSON type matched neither its numeric nor its string branch and the
    bound was never evaluated - the handler received a str/list/dict/bool for a
    declared `int`. A decoded frame carries real JSON types, so a contradicting
    one is refused, as the agent door already refuses a contradicting argument.
    """
    app = Veloce()
    seen: list = []

    @app.websocket_listener("/n")
    async def numbers(message: Annotated[int, Field(ge=1)]):
        seen.append(message)
        return {"got": message}

    with TestClient(app) as client, client.websocket_connect("/n") as ws:
        ws.send_json(frame)
        with pytest.raises(RuntimeError, match="1007"):
            ws.receive_json()
    assert seen == [], f"the handler was reached with {frame!r}"


def test_the_declared_type_still_accepts_its_own_frames():
    """The negative: the gate must not refuse what the signature asks for."""
    app = Veloce()

    @app.websocket_listener("/n")
    async def numbers(message: Annotated[int, Field(ge=1)]):
        return {"got": message}

    with TestClient(app) as client, client.websocket_connect("/n") as ws:
        ws.send_json(5)
        assert ws.receive_json() == {"got": 5}


def test_a_float_message_still_accepts_a_json_integer():
    """JSON has one number type, so a `float` declaration takes `2` as well as `2.5`."""
    app = Veloce()

    @app.websocket_listener("/f")
    async def ratio(message: Annotated[float, Field(ge=1)]):
        return {"got": message}

    with TestClient(app) as client, client.websocket_connect("/f") as ws:
        ws.send_json(2)
        assert ws.receive_json() == {"got": 2}
        ws.send_json(2.5)
        assert ws.receive_json() == {"got": 2.5}


def test_a_union_typed_message_accepts_every_member():
    """`str | int` means both, so the type gate must not pick one.

    The gate first peeled the union to its first member, so an `int` frame
    closed `1007` against a signature that explicitly allows it.
    """
    app = Veloce()
    seen: list = []

    @app.websocket_listener("/u")
    async def either(message: Annotated[str | int, Field(min_length=1)]):
        seen.append(message)
        return {"got": message}

    with TestClient(app) as client, client.websocket_connect("/u") as ws:
        ws.send_json("ab")
        assert ws.receive_json() == {"got": "ab"}
        ws.send_json(7)
        assert ws.receive_json() == {"got": 7}
    assert seen == ["ab", 7]


def test_a_union_typed_message_still_refuses_a_non_member():
    """The negative: widening to the union must not widen to anything else."""
    app = Veloce()
    seen: list = []

    @app.websocket_listener("/u")
    async def either(message: Annotated[str | int, Field(min_length=1)]):
        seen.append(message)
        return {"got": message}

    with TestClient(app) as client, client.websocket_connect("/u") as ws:
        ws.send_json([1])
        with pytest.raises(RuntimeError, match="1007"):
            ws.receive_json()
    assert seen == []


def test_a_union_typed_message_still_applies_the_bound():
    """And the bound still runs on the member that matched."""
    app = Veloce()

    @app.websocket_listener("/u")
    async def either(message: Annotated[str | int, Field(min_length=1)]):
        return {"got": message}

    with TestClient(app) as client, client.websocket_connect("/u") as ws:
        ws.send_json("")
        with pytest.raises(RuntimeError, match="1007"):
            ws.receive_json()


@pytest.mark.parametrize(
    "annotation,frame",
    [
        (Annotated[list[int], Field(min_length=1)], [1, 2, 3]),
        (Annotated[dict[str, int], Field(min_length=1)], {"a": 1}),
    ],
    ids=["list", "dict"],
)
def test_a_generic_typed_message_is_not_read_as_a_union(annotation, frame):
    """`get_args(list[int])` is `(int,)`, the same shape a union answers.

    Testing for args alone therefore checked a `[1, 2, 3]` frame against `int`
    and closed `1007` on every message such a listener could receive. The union
    is identified by its origin instead. Only scalar bounds are gated, so these
    shapes pass through as they did before the gate existed.
    """
    app = Veloce()
    seen: list = []

    async def listener(message=None):
        seen.append(message)
        return {"got": message}

    listener.__annotations__ = {"message": annotation, "return": dict}
    app.websocket_listener("/g")(listener)

    with TestClient(app) as client, client.websocket_connect("/g") as ws:
        ws.send_json(frame)
        assert ws.receive_json() == {"got": frame}
    assert seen == [frame]
