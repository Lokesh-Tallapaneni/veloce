"""Declarative WebSocket listener — the accept/receive/dispatch/close loop.

`Router.websocket_listener` wraps a per-message callback into a full WebSocket
handler. Nothing here reaches past the public `WebSocket` API.

`WebSocket` is imported at runtime, not under `TYPE_CHECKING`: the dependency
resolver reads the built handler's annotations through `get_type_hints`, which
evaluates them against this module's globals.
"""

from __future__ import annotations

import functools
import inspect
import types
import typing
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import typing_extensions

from veloce._handler_plan import _annotated_field_info, _AnnotationProbe, _callable_name
from veloce._internal import _is_async_callable, offload, resolve_hint_target
from veloce._model_backend import (
    ModelBackend,
    _msgspec,
    adapter_for,
    backend_of,
    is_msgspec_struct,
    resolve_response_contract,
)
from veloce._params import (
    ParamBase,
    field_info_declares_constraints,
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

    A sync callback is offloaded so a blocking per-message body never stalls the
    event loop, as a sync HTTP handler is.
    """
    wants_socket = _callback_wants_socket(callback)
    if _is_async_callable(callback):
        return callback, wants_socket

    async def _async_call(*args: Any) -> Any:
        # `offload` preserves the request-scoped ContextVars (`current_app`, `g`).
        return await offload(callback, *args)

    return _async_call, wants_socket


def _positional_params(callback: Any) -> list[inspect.Parameter]:
    """Return the callback's positional parameters, empty when it has no signature.

    Shared so `_message_annotation` and `_callback_wants_socket` cannot disagree
    about which parameter receives the message.
    """
    # `inspect.signature` unwraps a callable instance's `__call__` and drops `self`.
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

    True when the first positional parameter is named `ws` or `socket`, or when
    there are two or more. A single-parameter callback gets only the message.
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

    `None` when the callback declares nothing, which stays untyped and free.
    Read through `typing_extensions`, not `typing`: below Python 3.11 the stdlib
    drops the `Annotated` wrapper when the annotated type is a union, which is
    exactly the shape a discriminated message union has.
    """
    params = _positional_params(callback)
    if not params:
        return None
    target = params[1] if wants_socket and len(params) >= 2 else params[0]
    # Only this parameter: the whole signature would fail over an unrelated one.
    hint_target = resolve_hint_target(callback)
    declared = (getattr(hint_target, "__annotations__", None) or {}).get(target.name)
    if declared is None:
        return None
    probe = _AnnotationProbe()
    probe.__annotations__ = {target.name: declared}
    try:
        hints = typing_extensions.get_type_hints(
            probe, getattr(hint_target, "__globals__", None), include_extras=True
        )
    except Exception as exc:  # noqa: BLE001 - the name genuinely cannot be resolved
        # Refused, not warned: a dropped annotation would let every message reach
        # the callback unvalidated, and a message parameter has no default escape.
        raise TypeError(
            f"{_where(callback)}: could not resolve the annotation on "
            f"{target.name!r} ({type(exc).__name__}: {exc}). The listener is "
            "refused rather than registered unvalidated - define the message "
            "type at module level, or import it at runtime rather than only "
            "under TYPE_CHECKING."
        ) from exc
    return hints.get(target.name)


def _unwrap_annotated(annotation: Any) -> Any:
    """Return the type inside `Annotated[...]`, or the annotation unchanged."""
    return annotation.__origin__ if hasattr(annotation, "__metadata__") else annotation


def _message_union_members(inner: Any) -> tuple[Any, ...]:
    """Return a message union's model members, `Annotated` peeled, else empty.

    Left wrapped, a member satisfies neither backend's model predicate and the
    listener would silently end up unvalidated.
    """
    if typing_extensions.get_origin(inner) not in (typing.Union, types.UnionType):
        return ()
    members = tuple(
        _unwrap_annotated(a) for a in typing_extensions.get_args(inner) if a is not type(None)
    )
    if members and all(backend_of(m) is not ModelBackend.NONE for m in members):
        return members
    return ()


def _msgspec_tag_fields(members: tuple[Any, ...]) -> list[str | None]:
    """Each msgspec member's declared tag field, `None` where it declares none."""
    return [
        getattr(getattr(m, "__struct_config__", None), "tag_field", None)
        for m in members
        if is_msgspec_struct(m)
    ]


def _frame_matches(value: Any, target: Any) -> bool:
    """Whether a decoded frame has the JSON type its declared scalar asks for.

    Only the types `ParamBase.validate` branches on are gated. `bool` is tested
    before `int` because `isinstance(True, int)` is `True`. A union is satisfied
    by any member and is identified by its origin, not by having args -
    `get_args` answers `(int,)` for `list[int]` too.
    """
    if typing.get_origin(target) in (typing.Union, types.UnionType):
        members = [a for a in typing.get_args(target) if a is not type(None)]
        return any(_frame_matches(value, member) for member in members)
    if target is bool:
        return isinstance(value, bool)
    if target is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if target is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if target is Decimal:
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    if target is str:
        return isinstance(value, str)
    return True


def _bounded_scalar(marker: ParamBase, allow_null: bool, target: Any, data: Any) -> Any:
    """Apply a folded `Field`'s bounds to one scalar frame.

    Raising is the contract the caller expects: it answers a validator failure
    with a `1007` close. A `null` frame is handled here rather than left to
    `ParamBase.validate`, which would substitute the `Field`'s default. The
    declared type is checked before the bound, because `ParamBase.validate`
    dispatches on the runtime value; a frame contradicting it is refused, not
    coerced.
    """
    if data is None:
        if allow_null:
            return None
        raise ValueError("message may not be null")
    if not _frame_matches(data, target):
        raise ValueError(f"message must be a {getattr(target, '__name__', target)}")
    return marker.validate(data, "message")


def _message_validator(annotation: Any, inner: Any, members: tuple[Any, ...]) -> Any:
    """Return the callable that turns one decoded frame into a message.

    Selection only - the rules live in `_reject_ambiguous_union`. A union
    validates through the full annotation, so a `Model | None` `null` stays legal.
    """
    # `backend_of` answers "is this a message, and whose?" for both doors at once.
    backend = backend_of(members[0] if members else inner)
    if backend is ModelBackend.MSGSPEC:
        return _msgspec_validator(inner)
    if backend is ModelBackend.NONE:
        # Not a message model, but the annotation may still declare a bound, and
        # it is peeled the way the HTTP door peels it so `Optional` cannot hide it.
        field_info = _annotated_field_info(annotation)
        if field_info is not None and field_info_declares_constraints(field_info):
            # From the annotation: a non-empty `members` cannot reach this branch.
            allow_null = type(None) in typing.get_args(annotation) or type(None) in (
                typing.get_args(inner) or ()
            )
            # `inner` whole: `_frame_matches` peels `Optional` and unions itself.
            return functools.partial(
                _bounded_scalar,
                ParamBase.from_field_info(field_info),
                allow_null,
                inner,
            )
        return None
    # Pydantic validates its own models and adapts dataclasses / TypedDicts.
    return adapter_for(annotation).validate_python


def _reject_ambiguous_union(
    annotation: Any, members: tuple[Any, ...], discriminator: Any, callback: Any
) -> None:
    """Refuse a union that cannot be resolved to one message type.

    A union of two or more message types must be discriminated, and the tag is
    read from the declaration rather than off pydantic's compiled core schema,
    which hides it behind `definitions` for a self-referencing member. Every
    refusal is registration-time: a `1007` close would blame the peer for a
    fault in the server's own types.
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
    """Name the callback the way `_handler_plan` names a handler."""
    return _callable_name(callback)


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
    it, so the documented and the executed contract are one object.

    `members` is the union's members, or the single type in a one-tuple;
    `discriminator` is `None` when there is nothing to discriminate; `send_type`
    documents what the callback returns and filters nothing.
    """

    message_type: Any
    members: tuple[Any, ...]
    discriminator: str | None
    backend: ModelBackend
    validate: Callable[[Any], Any]
    send_type: Any = None


def _build_message_contract(callback: Any, wants_socket: bool) -> WSMessageContract | None:
    """Resolve the callback's message annotation into a contract, or `None`.

    Resolved exactly once - unwrapped, members listed, discriminator read - and
    every consumer is handed that one result.
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
        # A non-union is reported as a one-member tuple, so nothing special-cases it.
        members=members or (inner,),
        # The record carries the tag *field name*, which a callable one has none of.
        discriminator=declared if isinstance(declared, str) else None,
        backend=backend_of(members[0] if members else inner),
        validate=validate,
        send_type=resolve_response_contract(callback),
    )


def _declared_discriminator(annotation: Any, members: tuple[Any, ...]) -> Any:
    """Return the discriminator a union declares, or `None` when it declares none.

    A field name, or pydantic's callable `Discriminator`: pydantic carries it in
    the `Annotated` metadata, msgspec on `__struct_config__.tag_field`.
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

    The handler accepts, fires `on_connect`, then loops: receive in `receive`
    mode, pass to `callback`, send a non-`None` return in `send` mode. The loop
    ends on `WebSocketDisconnect`; `on_disconnect` always runs afterwards.
    """
    if receive not in _WS_MODES:
        raise ValueError(f"receive mode must be one of {sorted(_WS_MODES)}, got {receive!r}")
    if send not in _WS_MODES:
        raise ValueError(f"send mode must be one of {sorted(_WS_MODES)}, got {send!r}")

    fn, wants_socket = _resolve_listener_callable(callback)
    # Resolved at registration so an ambiguous union fails where it is written.
    # Only the JSON codec produces the mapping a model validates from.
    contract = _build_message_contract(callback, wants_socket) if receive == "json" else None
    validate = None if contract is None else contract.validate
    connect_fn = _resolve_listener_callable(on_connect)[0] if on_connect is not None else None
    disconnect_fn = (
        _resolve_listener_callable(on_disconnect)[0] if on_disconnect is not None else None
    )

    # Deliberately NOT `functools.wraps(callback)`: `__wrapped__` would send
    # `inspect.signature` back to `(data)` and the resolver would bind nothing.
    async def listener(ws: WebSocket) -> None:
        await ws.accept()
        try:
            if connect_fn is not None:
                await connect_fn(ws)
            while True:
                data = await _listener_receive(ws, receive)
                if validate is not None:
                    try:
                        data = validate(data)
                    except Exception:
                        # RFC 6455 Sec. 7.4.1: 1007 is "data inconsistent with the
                        # type of the message", so the peer learns why it closed.
                        await ws.close(
                            WS_1007_INVALID_FRAME_PAYLOAD_DATA, "message does not match schema"
                        )
                        break
                result = await (fn(ws, data) if wants_socket else fn(data))
                # A `None` return means "consume only" - emit no frame for it.
                if result is not None:
                    await _listener_send(ws, send, result)
        except WebSocketDisconnect:
            # The canonical, non-error way a listener loop terminates.
            pass
        finally:
            if disconnect_fn is not None:
                # Teardown runs even if the peer is already gone.
                await disconnect_fn(ws)

    # The callback's name without its signature (see the no-`wraps` note above).
    listener.__name__ = getattr(callback, "__name__", "listener")
    # On the handler, not `add_route`: every registration path re-registers it.
    listener._ws_message_contract = contract  # type: ignore[attr-defined]
    return listener, contract
