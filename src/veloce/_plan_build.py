"""Plan builders — turn a handler signature into a `HandlerPlan`."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, get_args, get_origin

from veloce._internal import _is_async_callable
from veloce._model_backend import ModelBackend, backend_of
from veloce._params import (
    ParamBase,
    field_info_declares_constraints,
    field_info_uncarried_constraints,
    needs_per_request_copy,
)
from veloce._plan_annotations import (
    _annotated_field_info,
    _callable_name,
    _group_field_specs,
    _guard_plain_mutable_default,
    _marker_kind,
    _raise_kwarg_ambiguity,
    _unwrap_list,
    _unwrap_optional,
    _warn_field_constraint_uncarried,
    _warn_field_info_overridden,
    _warn_field_info_unused,
    _warn_shared_mutable_default,
    extract_annotated_marker,
)
from veloce._plan_core import HandlerPlan
from veloce._plan_hints import _inspect_handler
from veloce._plan_kinds import (
    _NO_PATH_PARAMS,
    _SEQUENCE_ORIGINS,
    K_BG_TASKS,
    K_BODY_MODEL,
    K_DEPENDS,
    K_MODEL_GROUP,
    K_PARAM_MARKER,
    K_QUERY,
    K_QUERY_LIST,
    K_REQUEST,
    K_RESPONSE,
    K_SECURITY_SCOPES,
    K_UPLOAD_FILE,
    K_WEBSOCKET,
    MK_BODY,
    MK_COOKIE,
    MK_FILE,
    MK_FORM,
    MK_HEADER,
    MK_QUERY,
    _normalise_sequence_default,
)
from veloce._plan_slot import _Slot
from veloce.background import BackgroundTasks
from veloce.http.datastructures import UploadFile
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.websocket import WebSocket


def _build_depends_slot(
    name: str,
    dep: Any,
    inferred: Any = None,
    *,
    websocket: bool = False,
    path_params: frozenset[str] = _NO_PATH_PARAMS,
    _seen: list[Any] | None = None,
) -> _Slot:
    """Build a K_DEPENDS slot, recursively planning the sub-callable.

    `Depends()` with no explicit dependency falls back to `inferred`, the
    parameter's type annotation. `websocket` is threaded down the chain so a
    dependency of a WebSocket handler can itself receive the connection.
    """
    slot = _Slot(K_DEPENDS, name)
    slot.use_cache = dep.use_cache
    callable_ = dep.dependency if dep.dependency is not None else inferred
    slot.dep_callable = callable_
    slot.dep_is_coro = _is_async_callable(callable_)
    slot.dep_is_gen = inspect.isgeneratorfunction(callable_)
    slot.dep_is_async_gen = inspect.isasyncgenfunction(callable_)
    # Meaningful only for a plain sync callable; the rest have their own model.
    slot.dep_offload = bool(getattr(dep, "offload", False)) and not (
        slot.dep_is_coro or slot.dep_is_gen or slot.dep_is_async_gen
    )
    slot.sub_plan = build_plan(callable_, websocket=websocket, path_params=path_params, _seen=_seen)
    # Scopes flow down so a `SecurityScopes` parameter below sees the union.
    scopes = getattr(dep, "scopes", None)
    if scopes:
        # target_type is repurposed as the scope list for Security() slots.
        slot.target_type = list(scopes)
    # The cached result varies with the active scope union, so without this bit
    # one callable collides with itself.
    slot.scope_sensitive = _subgraph_reads_scopes(slot.sub_plan, set())
    return slot


def _subgraph_reads_scopes(plan: HandlerPlan | None, seen_plans: set[int]) -> bool:
    """Whether `plan` (or any dependency below it) reads `SecurityScopes`.

    Such a dependency's result depends on the active scope union, so its cache
    entry must be scope-keyed. Cycle-guarded via `seen_plans`.
    """
    if plan is None:
        return False
    plan_id = id(plan)
    if plan_id in seen_plans:
        return False
    seen_plans.add(plan_id)
    for sub in plan.slots:
        if sub.kind == K_SECURITY_SCOPES:
            return True
        if sub.kind == K_DEPENDS and _subgraph_reads_scopes(sub.sub_plan, seen_plans):
            return True
    return False


def _extend_plan_chain(handler: Callable[..., Any], seen: list[Any] | None) -> list[Any]:
    """Append `handler` to the chain being planned, refusing a `Depends` cycle."""
    if seen is None:
        return [handler]
    for previous in seen:
        if previous is handler:
            chain = [_callable_name(c) for c in [*seen, handler]]
            raise ValueError(f"Circular dependency detected: {' -> '.join(chain)}")
    return [*seen, handler]


def _build_marker_slot(
    param_name: str,
    marker: ParamBase,
    annotation: Any,
    *,
    has_default: bool,
    websocket: bool,
) -> _Slot | None:
    """Build the slot for an explicit Query/Path/Header/Cookie/Body/Form/File marker.

    Returns `None` when the marker reads a request body a WebSocket handshake
    does not have, leaving the parameter to its handler default.
    """
    marker_kind = _marker_kind(marker)
    if websocket and marker_kind in (MK_BODY, MK_FORM, MK_FILE):
        return None
    # A model under query/header/cookie/form groups that source's fields instead
    # of naming one key; `Body`/`File` stay whole-body.
    grp_opt, grp_inner = _unwrap_optional(annotation) if annotation else (False, annotation)
    grp_backend = backend_of(grp_inner) if grp_inner else ModelBackend.NONE
    if (
        getattr(marker, "group", False)
        and grp_backend is not ModelBackend.NONE
        and marker_kind
        in (
            MK_QUERY,
            MK_HEADER,
            MK_COOKIE,
            MK_FORM,
        )
    ):
        slot = _Slot(K_MODEL_GROUP, param_name)
        slot.marker = marker
        slot.marker_kind = marker_kind
        slot.model = grp_inner
        slot.backend = grp_backend
        slot.is_optional = grp_opt
        slot.has_default = marker.has_default
        slot.group_fields = _group_field_specs(grp_inner, grp_backend, marker_kind)
        return slot

    slot = _Slot(K_PARAM_MARKER, param_name)
    slot.marker = marker
    slot.marker_kind = marker_kind
    slot.lookup_name = marker.alias or param_name
    # An un-aliased Header param spells `x_token` as `x-token` unless disabled.
    if (
        slot.marker_kind == MK_HEADER
        and not marker.alias
        and getattr(marker, "convert_underscores", True)
    ):
        slot.lookup_name = slot.lookup_name.replace("_", "-")
    slot.default_factory = marker.default_factory
    slot.has_default = marker.has_default
    is_opt, inner = _unwrap_optional(annotation) if annotation else (False, annotation)
    slot.is_optional = is_opt
    slot.target_type = inner if is_opt else annotation
    # The origin test excludes the sequence defaults rebuilt below.
    if needs_per_request_copy(marker.default) and get_origin(slot.target_type) not in (
        set,
        frozenset,
        tuple,
    ):
        _warn_shared_mutable_default(param_name, marker.default, stacklevel=4)
    # `payload: Payload = Body()` declares what a bare `payload: Payload` does.
    if slot.marker_kind == MK_BODY and slot.target_type is not None:
        backend = backend_of(slot.target_type)
        if backend is not ModelBackend.NONE:
            slot.model = slot.target_type
            slot.backend = backend
    # Only a list-reading marker needs either.
    slot.marker_reads_list = get_origin(slot.target_type) in _SEQUENCE_ORIGINS
    if slot.marker_reads_list:
        marker = _normalise_sequence_default(marker, slot.target_type)
        slot.marker = marker
        from veloce.dependency import _list_marker_checks  # local import breaks the import cycle

        slot.list_checks = _list_marker_checks(marker)
        # `["0"]` bounded as a string would skip the published `items.minimum`.
        args = get_args(slot.target_type)
        slot.list_inner = args[0] if args else str
    return slot


def _adopt_field_marker(
    slot: _Slot,
    marker: ParamBase | None,
    param_name: str,
    declares_constraints: bool = False,
) -> None:
    """Carry a source-less `Field` marker on a bare path-or-query slot.

    The marker names no source, so the resolver keeps its own path-then-query
    lookup and only asks the marker to validate what that found. `lookup_name`
    is set either way, so the resolver reads one attribute per request.
    """
    slot.lookup_name = param_name
    if marker is None:
        return
    slot.marker = marker
    slot.marker_validates = declares_constraints
    if marker.alias:
        slot.lookup_name = marker.alias
    # The marker folded in the parameter's own default, so it is authoritative.
    slot.has_default = marker.has_default
    slot.default_factory = marker.default_factory
    slot._static_default = marker.default if marker.default is not ... else None
    if slot.kind == K_QUERY_LIST:
        # Only a list slot reads these; a scalar would hold a copy nothing reads.
        from veloce.dependency import _list_marker_checks  # local import breaks the import cycle

        slot.list_checks = _list_marker_checks(marker)


def _build_annotated_slot(
    param_name: str,
    annotation: Any,
    default: Any,
    *,
    has_default: bool,
    websocket: bool,
    field_info: Any = None,
    owner: str = "",
) -> _Slot | None:
    """Build the slot for a parameter carrying neither `Depends` nor a marker.

    The binding follows from the annotation alone. `None` when it needs a
    request body a WebSocket handshake does not have. A `field_info` is carried
    as a source-less marker by the two value bindings, and warned on by the two
    body bindings, where no marker could carry it.
    """
    is_optional, inner_type = _unwrap_optional(annotation) if annotation else (False, annotation)
    is_list, list_inner = _unwrap_list(inner_type) if inner_type else (False, inner_type)

    # A WebSocket has no multipart body, so the parameter keeps its default.
    if annotation is UploadFile or (is_optional and inner_type is UploadFile):
        if websocket:
            return None
        if field_info is not None and field_info_declares_constraints(field_info):
            _warn_field_info_unused(param_name, "a file upload", owner)
        slot = _Slot(K_UPLOAD_FILE, param_name)
        slot.is_optional = is_optional
        slot.has_default = has_default
        slot._static_default = default if has_default else None
        return slot

    # A `list[Model]` body is a GenericAlias, so it falls through to the list branch.
    body_backend = backend_of(inner_type) if inner_type else ModelBackend.NONE
    if body_backend is not ModelBackend.NONE:
        if websocket:
            return None
        if field_info is not None and field_info_declares_constraints(field_info):
            _warn_field_info_unused(param_name, "a request-body model", owner)
        slot = _Slot(K_BODY_MODEL, param_name)
        slot.model = inner_type
        slot.backend = body_backend
        slot.is_optional = is_optional
        slot.has_default = has_default
        slot._static_default = default if has_default else None
        return slot

    # A SOURCE-LESS marker: choosing `Path` or `Query` needs the route template.
    field_marker = (
        ParamBase.from_field_info(field_info, default if has_default else ...)
        if field_info is not None
        else None
    )
    field_declares_bounds = field_info is not None and field_info_declares_constraints(field_info)
    if field_info is not None:
        uncarried = field_info_uncarried_constraints(field_info)
        if uncarried:
            _warn_field_constraint_uncarried(param_name, uncarried, owner)

    if is_list:
        slot = _Slot(K_QUERY_LIST, param_name)
        slot.list_inner = list_inner
        slot.has_default = has_default
        slot._static_default = default if has_default else None
        slot.is_optional = is_optional
        _guard_plain_mutable_default(slot, param_name, stacklevel=4)
        _adopt_field_marker(slot, field_marker, param_name, field_declares_bounds)
        return slot

    # Decided at resolve time, because `path_params` are scope-local per match.
    slot = _Slot(K_QUERY, param_name)
    slot.target_type = inner_type if inner_type is not None else str
    slot.is_optional = is_optional
    slot.has_default = has_default
    slot._static_default = default if has_default else None
    _guard_plain_mutable_default(slot, param_name, stacklevel=4)
    _adopt_field_marker(slot, field_marker, param_name, field_declares_bounds)
    return slot


def build_plan(
    handler: Callable[..., Any],
    *,
    websocket: bool = False,
    path_params: frozenset[str] = _NO_PATH_PARAMS,
    _seen: list[Any] | None = None,
) -> HandlerPlan:
    """Inspect `handler` and freeze a resolution plan.

    Called once per route registration, and safe on builtins, lambdas, partials
    or callable classes - a handler with no inspectable signature gets an empty
    plan. With `websocket` set, a parameter typed `WebSocket` (or named `ws` /
    `websocket`) binds to a `K_WEBSOCKET` slot. `_seen` carries the chain being
    planned, so a `Depends` cycle raises `ValueError` rather than recursing.
    """
    from veloce.dependency import Depends, SecurityScopes  # local import breaks the import cycle

    _seen = _extend_plan_chain(handler, _seen)

    ws_type: Any = None
    if websocket:
        ws_type = WebSocket

    inspected = _inspect_handler(handler, path_params)
    if inspected is None:
        return HandlerPlan(handler, [], [])
    sig, hints = inspected

    slots: list[_Slot] = []
    positional_only: list[str] = []
    # The diagnostics below surface on one `router.py` line, which names no route.
    handler_name = _callable_name(handler)

    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue
        # *args / **kwargs are not injectable query parameters; a route that
        # reads path parameters generically (an endpoint-only `url_for` stub)
        # legitimately uses one, so this is skipped rather than rejected here -
        # the `"mcp"` plan extension (contrib/mcp/_plan_facts.py) is the one
        # consumer that cannot represent it and rejects it.
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        if param.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional_only.append(param_name)

        annotation = hints.get(param_name)
        default = param.default
        has_default = default is not inspect.Parameter.empty

        # PEP 593: a marker in the metadata is hoisted into the default position.
        field_info = _annotated_field_info(annotation)
        extracted_marker, annotation = extract_annotated_marker(annotation)
        if extracted_marker is not None and not isinstance(default, (Depends, ParamBase)):
            # `Annotated[int, Query()] = 10` declares its default beside the marker.
            if (
                has_default
                and isinstance(extracted_marker, ParamBase)
                and not extracted_marker.has_default
            ):
                extracted_marker = extracted_marker.with_default(default)
            default = extracted_marker
            has_default = True

        # A marker decides the parameter alone, so the `Field` has nothing to say.
        if field_info is not None and isinstance(default, (Depends, ParamBase)):
            if field_info_declares_constraints(field_info):
                _warn_field_info_overridden(param_name, default, handler_name)
            field_info = None

        # A by-name binding carrying a value marker would drop it silently.
        if isinstance(default, ParamBase):
            magic_name = param_name == "request" or (
                websocket and param_name in ("ws", "websocket")
            )
            if magic_name:
                _raise_kwarg_ambiguity(handler, param_name, default, _seen)

        # Checked first, so it wins over the request/path fallbacks below.
        if websocket and (annotation is ws_type or param_name in ("ws", "websocket")):
            slots.append(_Slot(K_WEBSOCKET, param_name))
            continue

        if param_name == "request" or annotation is Request:
            slots.append(_Slot(K_REQUEST, param_name))
            continue

        # A WebSocket handshake has no response cycle to attach tasks to.
        if annotation is BackgroundTasks:
            if not websocket:
                slots.append(_Slot(K_BG_TASKS, param_name))
            continue

        if annotation is Response:
            if not websocket:
                slots.append(_Slot(K_RESPONSE, param_name))
            continue

        if annotation is SecurityScopes:
            slots.append(_Slot(K_SECURITY_SCOPES, param_name))
            continue

        if isinstance(default, Depends):
            slots.append(
                _build_depends_slot(
                    param_name,
                    default,
                    inferred=annotation,
                    websocket=websocket,
                    path_params=path_params,
                    _seen=_seen,
                )
            )
            continue

        if isinstance(default, ParamBase):
            marker_slot = _build_marker_slot(
                param_name, default, annotation, has_default=has_default, websocket=websocket
            )
            if marker_slot is not None:
                slots.append(marker_slot)
            continue

        # A `Field` rides along as a source-less marker.
        annotated_slot = _build_annotated_slot(
            param_name,
            annotation,
            default,
            has_default=has_default,
            websocket=websocket,
            field_info=field_info,
            owner=handler_name,
        )
        if annotated_slot is not None:
            slots.append(annotated_slot)

    return HandlerPlan(handler, slots, [], tuple(positional_only))


def _variadic_param_error(handler_name: str, param_name: str, kind: Any) -> str:
    """Return the message for a `*args`/`**kwargs` handler parameter."""
    shape = "*" + param_name if kind is inspect.Parameter.VAR_POSITIONAL else "**" + param_name
    return (
        f"{handler_name}({shape}, ...): a handler parameter cannot be "
        f"*args or **kwargs - every argument must be named, since a request "
        f"carries named values, not an arbitrary positional/keyword tail."
    )


def split_positional_only(
    kwargs: dict[str, Any], positional_only: tuple[str, ...], handler: Callable[..., Any]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Pull `positional_only` names out of `kwargs`, in order, for a positional call.

    A `POSITIONAL_ONLY` parameter cannot be passed by keyword, so a caller
    invoking a resolved handler must call it as `handler(*positional, **kwargs)`
    rather than pure `handler(**kwargs)`. `kwargs` is mutated in place.

    Callers guard with `if positional_only:` first - the overwhelmingly
    common case has none, and a function call on every request to learn
    that measured ~40% slower than inlining the check at the call site.

    A name absent from `kwargs` has no slot at all - a `Body`/`Form`/`File`
    marker on a WebSocket handshake, which has no body to read one from -
    and takes `handler`'s own declared default for that parameter instead of
    raising `KeyError`. It cannot simply be left out: positional arguments
    bind by position, so omitting one would silently shift every later
    resolved name into the wrong parameter rather than raising.
    """
    positional: list[Any] = []
    sig: inspect.Signature | None = None
    for name in positional_only:
        if name in kwargs:
            positional.append(kwargs.pop(name))
            continue
        if sig is None:
            sig = inspect.signature(handler)
        positional.append(sig.parameters[name].default)
    return tuple(positional), kwargs


def build_route_dep_plans(
    route_dependencies: list[Any],
    *,
    websocket: bool = False,
    path_params: frozenset[str] = _NO_PATH_PARAMS,
) -> list[_Slot]:
    """Pre-plan a route's `dependencies=[Depends(...), ...]` list.

    A non-`Depends` entry is refused rather than skipped: dropped silently,
    `dependencies=[guard]` with the wrapper forgotten reads as protected and
    leaves every route open.
    """
    from veloce.dependency import Depends  # local import breaks the cycle

    out: list[_Slot] = []
    for dep in route_dependencies:
        if not isinstance(dep, Depends):
            raise TypeError(_dependency_entry_error(dep))
        out.append(_build_depends_slot("", dep, websocket=websocket, path_params=path_params))
    return out


def _dependency_entry_error(dep: Any) -> str:
    """Build the message for a `dependencies=` entry that is not a `Depends`."""
    if callable(dep):
        name = getattr(dep, "__name__", None) or "the callable"
        return (
            f"dependencies= takes Depends(...) or Security(...), not a bare callable; "
            f"got {name}. Wrap it as Depends({name}) so it runs."
        )
    return (
        f"dependencies= takes Depends(...) or Security(...); got {dep!r}, "
        f"which is not callable and cannot be a dependency."
    )
