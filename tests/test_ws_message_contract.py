"""The websocket message contract — one record, shared by the loop and any lowering."""

from __future__ import annotations

import dataclasses
from typing import Annotated, Literal

import pytest
import typing_extensions
from pydantic import BaseModel, Discriminator, Field, Tag

from tests._routes import route_at
from veloce import Veloce
from veloce._model_backend import ModelBackend
from veloce._route_contract import RouteContract
from veloce._ws_listener import WSMessageContract, build_listener_handler
from veloce.testclient import TestClient

try:
    import msgspec as _msgspec
except ImportError:  # pragma: no cover - exercised in the no-msgspec CI leg
    _msgspec = None

requires_msgspec = pytest.mark.skipif(_msgspec is None, reason="msgspec is not installed")

if _msgspec is not None:

    class MSay(_msgspec.Struct, tag="say", tag_field="type"):
        text: str

    class TypeTagged(_msgspec.Struct, tag="a", tag_field="type"):
        x: int

    class KindTagged(_msgspec.Struct, tag="b", tag_field="kind"):
        y: int


class Join(BaseModel):
    type: Literal["join"]
    room: str


class Say(BaseModel):
    type: Literal["say"]
    text: str


Inbound = Annotated[Join | Say, Field(discriminator="type")]

# An `Annotated` member is an alias, not a class - the union reader must peel it.
WrappedMembers = Annotated[Annotated[Join, Field()] | Say, Field(discriminator="type")]


def _pick_tag(value):
    return "join" if "room" in value else "say"


CallableDiscriminated = Annotated[
    Annotated[Join, Tag("join")] | Annotated[Say, Tag("say")], Discriminator(_pick_tag)
]


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


class Shared(BaseModel):
    a: int


class UsesSharedTwice(BaseModel):
    type: Literal["twice"]
    first: Shared
    second: Shared


SharedUnion = Annotated[UsesSharedTwice | Say, Field(discriminator="type")]


class Node(BaseModel):
    type: Literal["node"]
    child: Node | None = None


class Leaf(BaseModel):
    type: Literal["leaf"]
    v: int


RecursiveUnion = Annotated[Node | Leaf, Field(discriminator="type")]


def test_a_union_whose_member_reuses_a_submodel_is_accepted():
    """The discriminator comes from the declaration, not pydantic's schema shape."""

    async def chat(message: SharedUnion):
        return None

    _handler, contract = build_listener_handler(chat)
    assert contract.discriminator == "type"
    assert contract.members == (UsesSharedTwice, Say)


def test_a_union_with_a_self_referential_member_is_accepted():
    async def tree(message: RecursiveUnion):
        return None

    _handler, contract = build_listener_handler(tree)
    assert contract.discriminator == "type"


def test_a_reused_submodel_union_still_validates_each_frame():
    app = Veloce()

    @app.websocket_listener("/chat")
    async def chat(message: SharedUnion):
        return {"kind": type(message).__name__}

    with TestClient(app) as client, client.websocket_connect("/chat") as ws:
        ws.send_json({"type": "twice", "first": {"a": 1}, "second": {"a": 2}})
        assert ws.receive_json() == {"kind": "UsesSharedTwice"}
        ws.send_json({"type": "say", "text": "hi"})
        assert ws.receive_json() == {"kind": "Say"}
        ws.send_json({"type": "twice", "first": {"a": 1}})
        with pytest.raises(RuntimeError, match="1007"):
            ws.receive_json()


def test_an_optional_single_model_needs_no_discriminator_on_either_backend():
    """`Model | None` has one member, so there is nothing to discriminate."""

    async def pyd(message: Say | None):
        return None

    _handler, contract = build_listener_handler(pyd)
    assert contract.members == (Say,)
    assert contract.discriminator is None


@requires_msgspec
def test_an_optional_msgspec_struct_behaves_the_same_as_pydantic():
    async def msg(message: MSay | None):
        return None

    _handler, contract = build_listener_handler(msg)
    assert contract.members == (MSay,)
    assert contract.discriminator is None


def test_an_unresolvable_message_annotation_names_the_parameter():
    """The refusal must say which parameter failed, or it is not actionable.

    This replaced a test that pinned the *warning*'s `filename`. A warning is
    not a control: the listener it warned about went on to accept every frame
    unvalidated - a listener written against a model received raw dicts and
    failed on attribute access - so the warning became a `TypeError` and this
    pins the message the author now sees instead.
    """
    app = Veloce()

    with pytest.raises(TypeError) as caught:

        @app.websocket_listener("/local")
        async def local(message: StillMissing):  # noqa: F821
            return None

    assert "'message'" in str(caught.value)
    assert "TYPE_CHECKING" in str(caught.value)


def test_a_nested_callback_is_named_with_its_enclosing_context():
    """`_handler_plan` names a handler by `__qualname__`; this must agree."""
    app = Veloce()

    with pytest.raises(TypeError) as caught:

        @app.websocket_listener("/nested")
        async def nested(message: StillMissing):  # noqa: F821
            return None

    assert "test_a_nested_callback_is_named_with_its_enclosing_context.<locals>.nested" in str(
        caught.value
    )


def test_both_signature_readers_agree_on_a_callable_object():
    """One parameter filter, so `wants_socket` and the message pick cannot drift."""

    class Consumer:
        async def __call__(self, ws, message: Say):
            return {"text": message.text}

    _handler, contract = build_listener_handler(Consumer())
    assert contract is not None
    assert contract.message_type is Say


@requires_msgspec
def test_members_declaring_different_tag_fields_are_refused_at_registration():
    """No single field identifies the message, so it cannot fail on the frame."""

    async def f(message: TypeTagged | KindTagged):
        return None

    with pytest.raises(TypeError, match="different tag fields"):
        build_listener_handler(f)


def test_a_union_of_annotated_members_is_still_validated():
    """`Annotated[Model, ...]` is an alias, not a class; unwrap before testing."""

    async def chat(message: WrappedMembers):
        return None

    _handler, contract = build_listener_handler(chat)
    assert contract is not None, "an Annotated member left the listener unvalidated"
    assert contract.members == (Join, Say)
    assert contract.discriminator == "type"


def test_a_callable_discriminator_is_accepted_and_names_no_field():
    async def chat(message: CallableDiscriminated):
        return None

    _handler, contract = build_listener_handler(chat)
    assert contract is not None
    # Discriminated, but there is no single field to publish as the tag name.
    assert contract.discriminator is None
    assert contract.members == (Join, Say)


@dataclasses.dataclass
class Move:
    kind: Literal["move"]
    x: int


@dataclasses.dataclass
class Stop:
    kind: Literal["stop"]


AdaptedUnion = Annotated[Move | Stop, Field(discriminator="kind")]


@dataclasses.dataclass
class Undiscriminated:
    x: int


# `typing_extensions.TypedDict`, not `typing`'s: pydantic refuses the stdlib
# spelling below 3.12, which `_typeddict_is_adaptable` documents.
class Point(typing_extensions.TypedDict):
    x: int
    y: int


def test_a_dataclass_message_is_validated_like_an_http_body():
    """`backend_of` calls a dataclass ADAPTED; both doors must honour that."""
    app = Veloce()

    @app.websocket_listener("/move")
    async def move(data: Move):
        return {"type": type(data).__name__, "x": data.x}

    contract = route_at(app, "/move", include_hidden=True).ws_messages
    assert contract is not None, "a dataclass annotation left the listener unvalidated"
    assert contract.backend is ModelBackend.ADAPTED

    with TestClient(app) as client, client.websocket_connect("/move") as ws:
        ws.send_json({"kind": "move", "x": 1})
        assert ws.receive_json() == {"type": "Move", "x": 1}
        ws.send_json({"kind": "move", "x": "not-an-int"})
        with pytest.raises(RuntimeError, match="1007"):
            ws.receive_json()


def test_a_typeddict_message_is_validated():
    async def probe(data: Point):
        return None

    _handler, contract = build_listener_handler(probe)
    assert contract is not None
    assert contract.backend is ModelBackend.ADAPTED


def test_a_discriminated_dataclass_union_is_validated():
    async def probe(message: AdaptedUnion):
        return None

    _handler, contract = build_listener_handler(probe)
    assert contract is not None
    assert contract.members == (Move, Stop)
    assert contract.discriminator == "kind"


def test_an_undiscriminated_dataclass_union_is_refused():
    """Two dataclasses, so the rule is tested on every supported version.

    Pairing a dataclass with a `typing.TypedDict` made this pass only on 3.12+:
    below that pydantic refuses the stdlib spelling, so the union was not
    all-models and never reached the discriminator rule at all.
    """

    async def probe(message: Move | Undiscriminated):
        return None

    with pytest.raises(TypeError, match="must be discriminated"):
        build_listener_handler(probe)


def test_an_untyped_listener_still_registers():
    """POSITIVE: untyped listeners are a supported shape and must keep working."""
    app = Veloce()

    @app.websocket_listener("/echo")
    async def echo(data):
        return data

    with TestClient(app) as client, client.websocket_connect("/echo") as ws:
        ws.send_json({"free": "form"})
        assert ws.receive_json() == {"free": "form"}


def test_a_resolvable_message_annotation_still_validates():
    """POSITIVE: the fix must not disturb the contract it protects."""
    app = Veloce()

    @app.websocket_listener("/say")
    async def say(message: Say):
        return {"text": message.text}

    with TestClient(app) as client, client.websocket_connect("/say") as ws:
        ws.send_json({"type": "say", "text": "hi"})
        assert ws.receive_json() == {"text": "hi"}


def test_an_unresolvable_return_annotation_does_not_refuse_the_listener():
    """The refusal is about the message type, not every annotation in the signature.

    `_message_annotation` resolved the whole signature, so a return annotation
    that would not resolve refused the listener and blamed the message
    parameter - which resolved perfectly. `resolve_response_contract` is
    documented to tolerate an unresolvable return annotation and record no send
    contract, so the two resolvers disagreed about the same annotation.
    """
    app = Veloce()

    @app.websocket_listener("/chat")
    async def chat(ws, message: Say) -> NeverImportable:  # noqa: F821
        return None

    contract = route_at(app, "/chat", include_hidden=True).ws_messages
    assert contract.message_type is Say
    assert contract.send_type is None


def test_an_unresolvable_message_annotation_is_still_refused_alongside_a_bad_return():
    """NEGATIVE: narrowing to one parameter must not stop refusing the real case."""
    app = Veloce()

    with pytest.raises(TypeError, match="message"):

        @app.websocket_listener("/both")
        async def both(ws, message: AlsoMissing) -> NeverImportable:  # noqa: F821
            return None
