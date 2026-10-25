"""Route contract — the one IR projection every interface lowers from.

The frozen `HandlerPlan` built per handler at registration describes the route's
*contract*, not just how to run it. `RouteContract` is a thin read-only view over
it, and `iter_param_descriptors` the single canonical walk over its parameter
slots: OpenAPI and MCP both lower from that walk, so the documented contract and
the executed contract cannot drift.

The view holds references, never copies, and is built lazily by a lowering —
never at registration and never on the request path.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, get_type_hints

from veloce._handler_plan import (
    K_BODY_MODEL,
    K_DEPENDS,
    K_MODEL_GROUP,
    K_PARAM_MARKER,
    K_QUERY,
    K_QUERY_LIST,
    K_UPLOAD_FILE,
    MARKER_LOC,
    MK_BODY,
    MK_FILE,
    MK_FORM,
    build_plan,
)
from veloce._model_backend import ModelBackend, backend_of

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import HandlerPlan, _Slot
    from veloce._params import ParamBase
    from veloce._ws_listener import WSMessageContract
    from veloce.routing.router import RouteInfo


@dataclass(frozen=True, slots=True)
class ParamDescriptor:
    """One documentable handler input, projected from a plan slot.

    `location` is the wire source (`path`/`query`/`header`/`cookie`/`form`) or
    `body` for a JSON request-body model. `wire_name` is the name read off the
    wire - alias, or a header's hyphenated form - and `name` the Python
    parameter. `marker` is the originating `ParamBase`, carrying its constraints.
    """

    name: str
    wire_name: str
    location: str
    target_type: Any
    is_list: bool
    model: Any
    marker: ParamBase | None
    is_file: bool
    has_default: bool
    default: Any
    is_optional: bool
    #: A grouped model's `(validate_key, wire_key, is_list)` per field, since the
    #: group is N wire parameters rather than one. `None` for every other kind.
    group_fields: tuple[tuple[str, str, bool], ...] | None = None

    #: True when expanded from a group, where `model` names the field's own model
    #: so a lowering reads its declared schema rather than the bare annotation.
    group_field: bool = False


@dataclass(frozen=True, slots=True)
class RouteContract:
    """Read-only projection of a finalized route for non-runtime lowerings.

    Bundles the runtime IR (`plan`) with the route metadata a lowering reads.
    `ws_messages` is the typed websocket channel's message contract, or `None`
    when the route declares no messages. Built lazily via `from_route_info`,
    referencing the existing `HandlerPlan` rather than copying it.
    """

    plan: HandlerPlan
    param_names: tuple[str, ...]
    ws_messages: WSMessageContract | None = None

    @classmethod
    def from_route_info(cls, info: RouteInfo) -> RouteContract:
        """Project a finalized `RouteInfo` into a contract for lowering."""
        plan = info.handler_plan
        if plan is None:
            # Cold path: lowering reached before the plan was finalized, so it is
            # built from the same builder rather than read off the signature.

            plan = build_plan(info.handler, path_params=frozenset(info.param_names))
        return cls(
            plan=plan,
            param_names=tuple(info.param_names),
            ws_messages=info.ws_messages,
        )


def _is_model(annotation: Any) -> bool:
    """Whether `annotation` is a body-validatable model (Pydantic or msgspec)."""
    return annotation is not None and backend_of(annotation) is not ModelBackend.NONE


def describe_slot(slot: _Slot, param_names: tuple[str, ...]) -> ParamDescriptor | None:
    """Classify one plan slot into a `ParamDescriptor`, or `None` to skip it.

    The single per-slot classification every lowering shares. An inject-only slot
    (request, response, background tasks, security scopes, websocket, depends)
    carries no client- or agent-facing input and returns `None`. `param_names`
    resolves the planner's path-or-query ambiguity: a bare slot whose name is a
    path segment is a path parameter. A dependency's sub-graph is not recursed
    here; a lowering that advertises those walks `slot.sub_plan` itself.
    """
    kind = slot.kind
    if kind == K_PARAM_MARKER:
        mk = slot.marker_kind
        if mk == MK_BODY:
            location = "body"
            model = slot.target_type if _is_model(slot.target_type) else None
        elif mk in (MK_FORM, MK_FILE):
            location = "form"
            model = None
        else:
            location = MARKER_LOC[mk]
            model = None
        return ParamDescriptor(
            name=slot.name,
            wire_name=slot.lookup_name or slot.name,
            location=location,
            target_type=slot.target_type,
            is_list=False,
            model=model,
            marker=slot.marker,
            is_file=(mk == MK_FILE),
            has_default=slot.has_default,
            default=slot.marker.default if slot.marker is not None else None,
            is_optional=slot.is_optional,
        )
    if kind == K_BODY_MODEL:
        return ParamDescriptor(
            name=slot.name,
            wire_name=slot.name,
            location="body",
            target_type=slot.model,
            is_list=False,
            model=slot.model,
            marker=None,
            is_file=False,
            has_default=False,
            default=None,
            is_optional=slot.is_optional,
        )
    if kind == K_UPLOAD_FILE:
        return ParamDescriptor(
            name=slot.name,
            wire_name=slot.name,
            location="form",
            target_type=None,
            is_list=False,
            model=None,
            marker=None,
            is_file=True,
            has_default=slot.has_default,
            default=slot.default,
            is_optional=slot.is_optional,
        )
    if kind == K_QUERY_LIST:
        # A folded `Field(alias=...)` names the query key; a path keeps the
        # template's own name, which is what the radix tree stored.
        in_path = slot.name in param_names
        return ParamDescriptor(
            name=slot.name,
            wire_name=slot.name if in_path else (slot.lookup_name or slot.name),
            location="path" if in_path else "query",
            target_type=slot.list_inner,
            is_list=True,
            model=None,
            marker=slot.marker,
            is_file=False,
            has_default=slot.has_default,
            default=slot.default,
            is_optional=slot.is_optional,
        )
    if kind == K_QUERY:
        # A folded `Field(alias=...)` names the query key; a path keeps the
        # template's own name, which is what the radix tree stored.
        in_path = slot.name in param_names
        return ParamDescriptor(
            name=slot.name,
            wire_name=slot.name if in_path else (slot.lookup_name or slot.name),
            location="path" if in_path else "query",
            target_type=slot.target_type,
            is_list=False,
            model=None,
            marker=slot.marker,
            is_file=False,
            has_default=slot.has_default,
            default=slot.default,
            is_optional=slot.is_optional,
        )
    if kind == K_MODEL_GROUP:
        return ParamDescriptor(
            name=slot.name,
            wire_name=slot.name,
            location=MARKER_LOC.get(slot.marker_kind, "query"),
            target_type=slot.model,
            is_list=False,
            model=slot.model,
            marker=slot.marker,
            is_file=False,
            has_default=slot.has_default,
            default=None,
            is_optional=slot.is_optional,
            group_fields=slot.group_fields,
        )
    # Inject-only kinds are not documentable inputs.
    return None


def iter_param_descriptors(contract: RouteContract) -> Iterator[ParamDescriptor]:
    """Yield one `ParamDescriptor` per documentable input in the route's plan.

    The single canonical walk over the plan's parameter slots, built on
    `describe_slot`. Inject-only slots are skipped. Dependency sub-graphs *are*
    recursed, because a `Query()` declared on a dependency is read off the wire
    and enforced with a 422 exactly like one on the handler. A route with no
    dependency takes the flat loop, so the common case pays no bookkeeping.
    """
    param_names = contract.param_names
    slots = contract.plan.slots
    for slot in slots:
        if slot.kind == K_DEPENDS:
            yield from _iter_through_dependencies(slots, param_names)
            return
    for slot in slots:
        descriptor = describe_slot(slot, param_names)
        if descriptor is None:
            continue
        if descriptor.group_fields is not None:
            # A grouped model is N wire parameters, expanded here rather than in
            # each lowering, for the same reason the classification is shared.
            yield from _expand_group(descriptor)
            continue
        yield descriptor


def _iter_through_dependencies(
    slots: list[_Slot], param_names: tuple[str, ...]
) -> Iterator[ParamDescriptor]:
    """Walk a plan whose slots include a dependency, sub-graphs included.

    Level by level, so a handler's own parameter shadows a sub-dependency of the
    same name whatever the declaration order, and each sub-plan is visited once -
    a diamond graph is one wire parameter, not two.
    """
    seen_names: set[str] = set()
    seen_plans: set[int] = set()
    level = list(slots)
    while level:
        deeper: list[_Slot] = []
        for slot in level:
            if slot.kind == K_DEPENDS:
                sub_plan = slot.sub_plan
                if sub_plan is not None and id(sub_plan) not in seen_plans:
                    seen_plans.add(id(sub_plan))
                    deeper.extend(sub_plan.slots)
                continue
            descriptor = describe_slot(slot, param_names)
            if descriptor is None:
                continue
            expanded = (
                _expand_group(descriptor) if descriptor.group_fields is not None else (descriptor,)
            )
            for item in expanded:
                if item.wire_name not in seen_names:
                    seen_names.add(item.wire_name)
                    yield item
        level = deeper


def _expand_group(descriptor: ParamDescriptor) -> Iterator[ParamDescriptor]:
    """Yield one descriptor per field of a grouped model.

    Each field is read off the wire under its own name, so each is its own
    parameter to a lowering. Annotations come from the model, the only place
    they exist.
    """
    hints, defaults = _model_field_shape(descriptor.model)
    for validate_key, wire_key, is_list in descriptor.group_fields or ():
        # The model supplies a field's default when the key is absent, so the
        # wire parameter is optional exactly when the field has one.
        has_default = validate_key in defaults
        yield ParamDescriptor(
            name=validate_key,
            wire_name=wire_key,
            location=descriptor.location,
            target_type=hints.get(validate_key),
            is_list=is_list,
            model=descriptor.model,
            group_field=True,
            marker=None,
            is_file=False,
            has_default=has_default,
            default=defaults.get(validate_key),
            is_optional=descriptor.is_optional or has_default,
        )


def _model_field_shape(model: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """`({field: annotation}, {field: default})` for any backend the planner accepts.

    A field with no default is absent from the second mapping, which is what
    tells a lowering the parameter is required.
    """
    hints: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    fields = getattr(model, "model_fields", None)
    if fields is not None:
        for name, field in fields.items():
            # The key the group walk validates under: for an alias, the alias.
            key = getattr(field, "alias", None) or name
            hints[key] = getattr(field, "annotation", None)
            if not getattr(field, "is_required", lambda: True)():
                defaults[key] = getattr(field, "default", None)
        return hints, defaults
    try:
        hints = dict(get_type_hints(model))
    except Exception:
        return {}, {}
    for name in hints:
        if hasattr(model, name):
            defaults[name] = getattr(model, name)
    return hints, defaults
