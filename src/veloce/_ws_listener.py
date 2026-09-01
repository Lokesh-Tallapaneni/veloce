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
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import typing_extensions

from veloce._internal import _is_async_callable, offload
from veloce._model_backend import (
    ModelBackend,
    _is_model_union,
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


def _callback_wants_socket(callback: Any) -> bool:
    """Decide whether a listener callback expects the socket as its first arg.

    True when the first positional parameter is named `ws` or `socket`, or
    when the callback accepts two or more positional parameters (so the data
    is the second). A single-parameter `on_receive(data)` callback gets only
    the message.
    """
    # `inspect.signature` already unwraps a callable instance's `__call__`
    # and drops the bound `self`, so it works on plain functions, bound
    # methods, and `__call__`-able objects alike.
    try:
        params = [
            p
            for p in inspect.signature(callback).parameters.values()
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        return False
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
    try:
        params = [
            p
            for p in inspect.signature(callback).parameters.values()
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        return None
    if not params:
        return None
    target = params[1] if wants_socket and len(params) >= 2 else params[0]
    try:
        hints = typing_extensions.get_type_hints(callback, include_extras=True)
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
            stacklevel=4,
        )
        return None
    return hints.get(target.name)


def _unwrap_annotated(annotation: Any) -> Any:
    """Return the type inside `Annotated[...]`, or the annotation unchanged."""
    return annotation.__origin__ if hasattr(annotation, "__metadata__") else annotation


def _union_members(tp: Any) -> tuple[Any, ...]:
    return tuple(a for a in typing_extensions.get_args(tp) if a is not type(None))


def _build_message_validator(annotation: Any, callback: Any) -> Any:
    """Return a per-frame validator for `annotation`, or `None` when untyped.

    A union of message types must be discriminated, and the framework does not
    invent the discriminator - it asks the backend, which already has the rule.
    msgspec refuses an untagged struct union itself, so building the converter
    is the check. Pydantic does not refuse it: it resolves an untagged union by
    first match, so two structurally identical messages would route by
    declaration order. Its core schema names the kind it built, so the same rule
    comes from there.

    The refusal happens at registration rather than on the first ambiguous
    frame, matching how the rest of the codebase treats an ambiguous
    declaration.
    """
    inner = _unwrap_annotated(annotation)
    if inner is None or inner is Any:
        return None

    if _is_model_union(inner):
        members = _union_members(inner)
        structs = sum(1 for m in members if is_msgspec_struct(m))
        if structs and structs != len(members):
            raise TypeError(
                f"{_where(callback)}: a websocket message union must use one model "
                f"backend, and {annotation!r} mixes msgspec structs with pydantic "
                "models. Neither backend can validate the other's members."
            )
        if structs:
            # msgspec's own rule - "all Struct types must be tagged" - so
            # building the converter is the check.
            try:
                _msgspec.convert({}, inner)
            except TypeError as exc:
                raise TypeError(_undiscriminated(callback, annotation, str(exc))) from exc
            except Exception:
                # A validation failure on the empty probe means the union is
                # well-formed, which is all this checks.
                pass
            return _msgspec_validator(inner)
        adapter = adapter_for(annotation)
        if adapter.core_schema.get("type") != "tagged-union":
            raise TypeError(
                _undiscriminated(
                    callback,
                    annotation,
                    "pydantic resolves an untagged union by first match, so two "
                    "messages with the same shape would route by declaration order",
                )
            )
        return adapter.validate_python

    if is_msgspec_struct(inner):
        return _msgspec_validator(inner)
    if is_pydantic_model(inner):
        return adapter_for(annotation).validate_python
    return None


def _msgspec_validator(inner: Any) -> Any:
    convert = _msgspec.convert

    def validate(payload: Any) -> Any:
        return convert(payload, inner)

    return validate


def _where(callback: Any) -> str:
    return getattr(callback, "__name__", None) or "websocket listener"


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
    """Resolve the callback's message annotation into a contract, or `None`."""
    annotation = _message_annotation(callback, wants_socket)
    if annotation is None:
        return None
    validate = _build_message_validator(annotation, callback)
    if validate is None:
        return None
    inner = _unwrap_annotated(annotation)
    members = _union_members(inner) if _is_model_union(inner) else (inner,)
    return WSMessageContract(
        message_type=annotation,
        members=members,
        discriminator=_discriminator_of(annotation, members),
        backend=backend_of(members[0]),
        validate=validate,
        send_type=resolve_response_contract(callback),
    )


def _discriminator_of(annotation: Any, members: tuple[Any, ...]) -> str | None:
    """Return the tag field a union is discriminated on, or `None`.

    Asked of the backend that owns the members rather than re-derived: pydantic
    records it in the `Field(discriminator=...)` metadata, msgspec on the struct
    as `__struct_tag_field__`.
    """
    if len(members) < 2:
        return None
    for meta in getattr(annotation, "__metadata__", ()):
        field = getattr(meta, "discriminator", None)
        if field:
            return str(field)
    return getattr(members[0], "__struct_tag_field__", None)


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
