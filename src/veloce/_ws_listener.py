"""Declarative WebSocket listener — the accept/receive/dispatch/close loop.

`Router.websocket_listener` wraps a per-message callback into a full WebSocket
handler: accept, receive-loop, dispatch, clean disconnect. The loop builder
lives here rather than in `routing/` so the router stays free of WebSocket
internals, and rather than in `websocket.py` so the one module a maintainer
opens to change frame parsing is not also the one they open to change
listener-callback binding. Nothing here reaches past the public `WebSocket`
API.

`WebSocket` is imported at runtime, not under `TYPE_CHECKING`: the dependency
resolver reads the built handler's annotations through `get_type_hints`, which
evaluates them against this module's globals.
"""

from __future__ import annotations

import inspect
import types
import typing
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import typing_extensions

from veloce._internal import _is_async_callable, offload
from veloce._model_backend import (
    ModelBackend,
    _msgspec,
    adapter_for,
    backend_of,
    is_msgspec_struct,
    is_pydantic_model,
    resolve_response_contract,
)
from veloce.exceptions import WebSocketDisconnect
from veloce.status import WS_1007_INVALID_FRAME_PAYLOAD_DATA
from veloce.websocket import WebSocket

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable, Callable, Coroutine


_WS_MODES = frozenset({"text", "bytes", "json"})


def _resolve_listener_callable(
    callback: Any,
) -> tuple[Callable[..., Awaitable[Any]], bool]:
    """Return an async-callable form of `callback` and whether it wants the socket.

    A sync callback is offloaded to the default executor so a blocking
    per-message body never stalls the event loop, matching how the framework
    runs sync HTTP handlers. The socket is passed positionally as the first
    argument when the callback declares a leading `ws`/`socket` parameter or
    accepts two or more positional parameters.
    """
    wants_socket = _callback_wants_socket(callback)
    if _is_async_callable(callback):
        return callback, wants_socket

    async def _async_call(*args: Any) -> Any:
        # `offload` preserves the request-scoped ContextVars a sync HTTP
        # handler sees (`current_app` / `g` / `request`).
        return await offload(callback, *args)

    return _async_call, wants_socket


def _hint_target(callback: Any) -> Any:
    """Return the object whose annotations describe `callback`'s parameters.

    `inspect.signature` reads a callable instance through its `__call__` and
    drops the bound `self`, but `get_type_hints` on the instance answers about
    the *class attributes* instead - so the parameter list and the annotations
    would come from two different objects and a callable-object listener's
    message type would silently never be read.
    """
    if inspect.isroutine(callback):
        return callback
    return type(callback).__call__ if callable(callback) else callback


def _positional_params(callback: Any) -> list[inspect.Parameter]:
    """Return the callback's positional parameters, empty when it has no signature.

    Shared so the two readers cannot disagree about which parameters count:
    `_message_annotation` takes `params[1]` exactly when `_callback_wants_socket`
    saw two, so a filter that drifted between them would leave the listener
    validating the wrong argument with nothing raising.
    """
    # `inspect.signature` already unwraps a callable instance's `__call__`
    # and drops the bound `self`, so it works on plain functions, bound
    # methods, and `__call__`-able objects alike.
    try:
        return [
            p
            for p in inspect.signature(callback).parameters.values()
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        return []


def _callback_wants_socket(callback: Any) -> bool:
    """Decide whether a listener callback expects the socket as its first arg.

    True when the first positional parameter is named `ws` or `socket`, or
    when the callback accepts two or more positional parameters (so the data
    is the second). A single-parameter `on_receive(data)` callback gets only
    the message.
    """
    params = _positional_params(callback)
    if not params:
        return False
    if params[0].name in ("ws", "socket"):
        return True
    return len(params) >= 2


async def _listener_receive(ws: WebSocket, mode: str) -> Any:
    if mode == "text":
        return await ws.receive_text()
    if mode == "bytes":
        return await ws.receive_bytes()
    return await ws.receive_json()


async def _listener_send(ws: WebSocket, mode: str, data: Any) -> None:
    if mode == "text":
        await ws.send_text(data if isinstance(data, str) else str(data))
    elif mode == "bytes":
        await ws.send_bytes(data)
    else:
        await ws.send_json(data)


def _message_annotation(callback: Any, wants_socket: bool) -> Any:
    """Return the annotation on the parameter that receives the message.

    `None` when the callback declares nothing, which is the existing untyped
    behaviour and stays free.

    Read through `typing_extensions`, not `typing`: below Python 3.11 the stdlib
    drops the `Annotated` wrapper when the annotated type is a union, and
    `Annotated[Join | Say, Field(discriminator="type")]` is exactly that shape -
    so the discriminator would silently vanish on 3.10 and the union would be
    refused as ambiguous on the one interpreter where that is hardest to debug.
    """
    params = _positional_params(callback)
    if not params:
        return None
    target = params[1] if wants_socket and len(params) >= 2 else params[0]
    try:
        hints = typing_extensions.get_type_hints(_hint_target(callback), include_extras=True)
    except Exception as exc:  # noqa: BLE001 - the name genuinely cannot be resolved
        # Never silent: an unresolved annotation means the frames the callback
        # was promised would be validated arrive raw instead, and a listener
        # written against a model would fail on attribute access rather than
        # at the boundary. Same reason the handler plan warns.
        warnings.warn(
            f"{_where(callback)}: could not resolve the annotation on "
            f"{target.name!r} ({type(exc).__name__}: {exc}); messages are passed "
            "through unvalidated - define the message type at module level, or "
            "import it at runtime rather than only under TYPE_CHECKING",
            # `_message_annotation` -> `_build_message_contract` ->
            # `build_listener_handler` -> the router's decorator -> the user's
            # `@app.websocket_listener` line, which is the only frame worth
            # pointing at. Pinned by a test, because adding a helper in between
            # silently moves it.
            stacklevel=5,
        )
        return None
    return hints.get(target.name)


def _unwrap_annotated(annotation: Any) -> Any:
    """Return the type inside `Annotated[...]`, or the annotation unchanged."""
    return annotation.__origin__ if hasattr(annotation, "__metadata__") else annotation


def _message_union_members(inner: Any) -> tuple[Any, ...]:
    """Return a message union's model members, `Annotated` peeled, else empty.

    Members are unwrapped because `Annotated[Join, Tag("join")]` is an alias,
    not a class: left wrapped it satisfies neither backend's model predicate,
    the union reads as "not a union of models", and the listener silently ends
    up unvalidated - the one failure direction this module must not have.
    """
    if typing_extensions.get_origin(inner) not in (typing.Union, types.UnionType):
        return ()
    members = tuple(
        _unwrap_annotated(a) for a in typing_extensions.get_args(inner) if a is not type(None)
    )
    if members and all(is_pydantic_model(m) or is_msgspec_struct(m) for m in members):
        return members
    return ()


def _msgspec_tag_fields(members: tuple[Any, ...]) -> list[str | None]:
    """Each msgspec member's declared tag field, `None` where it declares none."""
    return [
        getattr(getattr(m, "__struct_config__", None), "tag_field", None)
        for m in members
        if is_msgspec_struct(m)
    ]


def _message_validator(annotation: Any, inner: Any, members: tuple[Any, ...]) -> Any:
    """Return the callable that turns one decoded frame into a message.

    Selection only - the rules live in `_reject_ambiguous_union`, and the
    annotation is already resolved by the caller. A union validates through the
    full annotation rather than through `inner`, so a `Model | None` frame of
    `null` stays legal: the author asked for that by writing the `| None`.
    """
    single = members[0] if members else inner
    if is_msgspec_struct(single):
        return _msgspec_validator(inner)
    if is_pydantic_model(single):
        return adapter_for(annotation).validate_python
    return None


def _reject_ambiguous_union(
    annotation: Any, members: tuple[Any, ...], discriminator: Any, callback: Any
) -> None:
    """Refuse a union that cannot be resolved to one message type.

    A union of two or more message types must be discriminated: a frame has to
    become exactly one of them, and nothing else can make that choice. The tag
    is read from the declaration, which is the one place it is stated and the
    one place both backends agree on.

    Deliberately not read off pydantic's compiled core schema. That reports
    `tagged-union` only for the simplest shape: a member referencing one
    submodel twice, or referencing itself, makes pydantic wrap the schema in
    `definitions` and the tag stops being visible there - so a correctly
    discriminated union would be refused at registration, told to add the
    discriminator it already declared.

    Every refusal here is registration-time. A declaration that only fails on
    the first frame would surface as a `1007` close, which tells the peer its
    message was malformed when the fault is in the server's own types.
    """
    if len(members) < 2:
        return
    structs = sum(1 for m in members if is_msgspec_struct(m))
    if structs and structs != len(members):
        raise TypeError(
            f"{_where(callback)}: a websocket message union must use one model "
            f"backend, and {annotation!r} mixes msgspec structs with pydantic "
            "models. Neither backend can validate the other's members."
        )
    tag_fields = _msgspec_tag_fields(members)
    if len(set(tag_fields)) > 1:
        named = ", ".join(repr(f) for f in dict.fromkeys(tag_fields))
        raise TypeError(
            f"{_where(callback)}: the members of {annotation!r} declare different "
            f"tag fields ({named}), so no single field identifies the message. "
            "Give every member the same `tag_field`."
        )
    if discriminator is None:
        raise TypeError(
            _undiscriminated(
                callback,
                annotation,
                "without a tag the frame is matched against the members in "
                "declaration order, so two messages with the same shape "
                "would be indistinguishable",
            )
        )


def _msgspec_validator(inner: Any) -> Any:
    convert = _msgspec.convert

    def validate(payload: Any) -> Any:
        return convert(payload, inner)

    return validate


def _where(callback: Any) -> str:
    """Name the callback the way `_handler_plan` names a handler.

    `__qualname__` first, so a listener defined as a method or inside a factory
    keeps its enclosing context - the sibling warning in `_handler_plan`
    resolves it that way, and two different answers to "which callback is
    this?" for the same class of failure is worse than either answer.
    """
    return (
        getattr(callback, "__qualname__", None)
        or getattr(callback, "__name__", None)
        or repr(callback)
    )


def _undiscriminated(callback: Any, annotation: Any, detail: str) -> str:
    return (
        f"{_where(callback)}: a websocket message union must be discriminated, and "
        f"{annotation!r} is not. A frame has to become exactly one of these types, "
        "so something must choose - tag the members (`msgspec.Struct(tag=...)`, or "
        "a `Literal` field selected with `Field(discriminator=...)`) rather than "
        f"leaving the choice to declaration order. {detail}"
    )


@dataclass(frozen=True, slots=True)
class WSMessageContract:
    """What one websocket channel accepts, resolved once at registration.

    The receive loop validates through this record and a lowering publishes from
    it, so the documented contract and the executed contract are the same object
    rather than two resolutions of one annotation.

    `members` is the union's members, or the single type in a one-tuple.
    `discriminator` is `None` when there is nothing to discriminate.

    `send_type` documents what the callback returns and filters nothing - the
    same rule response-model unions already follow, because which member a value
    should be re-shaped through is ambiguous. The receive side must choose and
    so demands a discriminator; the send side never chooses.
    """

    message_type: Any
    members: tuple[Any, ...]
    discriminator: str | None
    backend: ModelBackend
    validate: Callable[[Any], Any]
    send_type: Any = None


def _build_message_contract(callback: Any, wants_socket: bool) -> WSMessageContract | None:
    """Resolve the callback's message annotation into a contract, or `None`.

    The annotation is resolved exactly once - unwrapped, its union members
    listed, its discriminator read - and every consumer is handed that result.
    Resolving a second time per consumer is how two copies drift: they are pure
    functions of one annotation, so a repeat evaluation can only agree with the
    first or be a bug in waiting.
    """
    annotation = _message_annotation(callback, wants_socket)
    if annotation is None:
        return None
    inner = _unwrap_annotated(annotation)
    if inner is None or inner is Any:
        return None
    members = _message_union_members(inner)
    declared = _declared_discriminator(annotation, members)
    _reject_ambiguous_union(annotation, members, declared, callback)
    validate = _message_validator(annotation, inner, members)
    if validate is None:
        return None
    return WSMessageContract(
        message_type=annotation,
        # A non-union names one message; report it the same way a one-member
        # union does, so a consumer never special-cases the shape.
        members=members or (inner,),
        # The record carries the tag *field name*; pydantic's callable
        # `Discriminator` declares a discriminator with no field to name.
        discriminator=declared if isinstance(declared, str) else None,
        backend=backend_of(members[0] if members else inner),
        validate=validate,
        send_type=resolve_response_contract(callback),
    )


def _declared_discriminator(annotation: Any, members: tuple[Any, ...]) -> Any:
    """Return the discriminator a union declares, or `None` when it declares none.

    A field name, or pydantic's callable `Discriminator`. Read from the
    declaration, which states it once: pydantic carries it in the `Annotated`
    metadata, msgspec on the struct's `__struct_config__.tag_field`. Both
    lookups are open by necessity - the annotation may not be `Annotated`, and
    a member may be either backend's model.

    One member is one message type, so there is nothing to choose between.
    """
    if len(members) < 2:
        return None
    for meta in getattr(annotation, "__metadata__", ()):
        field = getattr(meta, "discriminator", None)
        if field:
            return field
    tag_fields = _msgspec_tag_fields(members)
    return tag_fields[0] if tag_fields and all(tag_fields) else None


def build_listener_handler(
    callback: Any,
    *,
    receive: str = "json",
    send: str = "json",
    on_connect: Any = None,
    on_disconnect: Any = None,
) -> tuple[Callable[[WebSocket], Coroutine[Any, Any, None]], WSMessageContract | None]:
    """Build a WebSocket handler that runs the canonical accept/receive/close loop.

    The returned handler accepts the connection, fires `on_connect`, then
    loops: receive one message in `receive` mode, pass it to `callback`, and
    send the return value in `send` mode when it is not `None`. The loop ends
    on `WebSocketDisconnect`; `on_disconnect` always runs afterwards. A
    callback that returns `None` sends nothing, so a pure consumer needs no
    special casing.
    """
    if receive not in _WS_MODES:
        raise ValueError(f"receive mode must be one of {sorted(_WS_MODES)}, got {receive!r}")
    if send not in _WS_MODES:
        raise ValueError(f"send mode must be one of {sorted(_WS_MODES)}, got {send!r}")

    fn, wants_socket = _resolve_listener_callable(callback)
    # Resolved once, at registration: the annotation cannot change, and an
    # ambiguous union must fail where it is written rather than on the frame
    # that happens to be ambiguous.
    # Only the JSON codec produces the mapping a model validates from; a
    # `text`/`bytes` listener receives the frame as-is and declares no model.
    contract = _build_message_contract(callback, wants_socket) if receive == "json" else None
    validate = None if contract is None else contract.validate
    connect_fn = _resolve_listener_callable(on_connect)[0] if on_connect is not None else None
    disconnect_fn = (
        _resolve_listener_callable(on_disconnect)[0] if on_disconnect is not None else None
    )

    # Deliberately NOT `functools.wraps(callback)`: the registered handler
    # must present its own `(ws: WebSocket)` signature so the dependency
    # resolver injects the socket. `wraps` sets `__wrapped__`, which
    # `inspect.signature` follows back to the callback's `(data)` shape and
    # makes the resolver try to bind a nonexistent `data` dependency.
    async def listener(ws: WebSocket) -> None:
        await ws.accept()
        try:
            if connect_fn is not None:
                await connect_fn(ws)
            while True:
                data = await _listener_receive(ws, receive)
                # One `is None` check for an unannotated callback, which keeps
                # the untyped path exactly as it was.
                if validate is not None:
                    try:
                        data = validate(data)
                    except Exception:
                        # RFC 6455 Sec. 7.4.1: 1007 is "data inconsistent with
                        # the type of the message", which is exactly a frame
                        # that does not match the declared contract. Closing
                        # beats raising - the peer learns why, and the handler
                        # never sees a frame it was promised could not arrive.
                        await ws.close(
                            WS_1007_INVALID_FRAME_PAYLOAD_DATA, "message does not match schema"
                        )
                        break
                result = await (fn(ws, data) if wants_socket else fn(data))
                # A `None` return means "consume only" - never emit a frame
                # for it (sending `null`/empty would be a spurious message).
                if result is not None:
                    await _listener_send(ws, send, result)
        except WebSocketDisconnect:
            # Peer (or idle/heartbeat close) ended the connection - the
            # canonical, non-error way a listener loop terminates.
            pass
        finally:
            if disconnect_fn is not None:
                # Run teardown even if the peer is already gone; a send from
                # inside `on_disconnect` may itself raise, which is fine.
                await disconnect_fn(ws)

    # Borrow the callback's name for routing/OpenAPI introspection without
    # importing its signature (see the no-`wraps` note above).
    listener.__name__ = getattr(callback, "__name__", "listener")
    # Carried on the handler rather than passed to `add_route`: every
    # registration path - direct, blueprint splice, router merge - re-registers
    # this same object, so the contract follows it without a forwarding line
    # each copy could forget. `_finalize_plans` reads it off the handler the
    # same way it builds the handler plan.
    listener._ws_message_contract = contract  # type: ignore[attr-defined]
    return listener, contract
