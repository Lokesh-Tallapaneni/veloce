"""Route contract — the one IR projection every interface lowers from.

Veloce builds a frozen `HandlerPlan` per handler at registration and compiles it
to the runtime resolver. The same plan describes each route's *contract*, not
just how to run it. `RouteContract` is a thin read-only view over that plan plus
the route metadata the non-runtime lowerings need, and `iter_param_descriptors`
is the single canonical walk over the plan's parameter slots. OpenAPI lowers
from this walk (as the MCP layer already does), so the documented contract and
the executed contract cannot drift.

The view holds references, never copies, and is built lazily by a lowering —
never at registration and never on the request path, so the resolver and its
slot layout are untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from veloce._handler_plan import (
    K_BODY_MODEL,
    K_PARAM_MARKER,
    K_PATH,
    K_QUERY,
    K_QUERY_LIST,
    K_UPLOAD_FILE,
    MARKER_LOC,
    MK_BODY,
    MK_FILE,
    MK_FORM,
)
from veloce._model_backend import ModelBackend, backend_of

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import HandlerPlan
    from veloce.routing.params import ParamBase
    from veloce.routing.router import RouteInfo


@dataclass(frozen=True, slots=True)
class ParamDescriptor:
    """One documentable handler input, projected from a plan slot.

    `location` is the wire source (`path`/`query`/`header`/`cookie`/`form`) or
    `body` for a JSON request-body model. `wire_name` is the name read off the
    wire (alias, or a header's hyphenated form); `name` is the Python parameter
    name. `marker` is the originating `ParamBase` when one drove the slot, so a
    lowering can read its constraints directly.
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


@dataclass(frozen=True, slots=True)
class RouteContract:
    """Read-only projection of a finalized route for non-runtime lowerings.

    Bundles the runtime IR (`plan`) with the route metadata a lowering reads.
    Built lazily by a lowering via `from_route_info`; references the existing
    `HandlerPlan` and never copies it. Grows fields as further lowerings (the
    typed client) need them.
    """

    plan: HandlerPlan
    param_names: tuple[str, ...]

    @classmethod
    def from_route_info(cls, info: RouteInfo) -> RouteContract:
        """Project a finalized `RouteInfo` into a contract for lowering."""
        plan = info.handler_plan
        if plan is None:
            # Cold path: a caller reached lowering before the route's plan was
            # finalized. Build it on demand from the same builder registration
            # uses, so the lowering still reads the IR rather than the signature.
            from veloce._handler_plan import build_plan  # cold path, no cycle at runtime

            plan = build_plan(info.handler)
        return cls(plan=plan, param_names=tuple(info.param_names))


def _is_model(annotation: Any) -> bool:
    """Whether `annotation` is a body-validatable model (Pydantic or msgspec)."""
    return annotation is not None and backend_of(annotation) is not ModelBackend.NONE


def iter_param_descriptors(contract: RouteContract) -> Iterator[ParamDescriptor]:
    """Yield one `ParamDescriptor` per documentable input in the route's plan.

    The single canonical walk over the plan's parameter slots. Inject-only slots
    (request, response, background tasks, security scopes, websocket, depends)
    carry no client- or agent-facing input and are skipped, exactly as the
    resolver-facing interpreter skips them. A dependency's own sub-graph is not
    recursed here; a lowering that advertises sub-dependency inputs walks
    `slot.sub_plan` itself.
    """
    param_names = contract.param_names
    for slot in contract.plan.slots:
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
            yield ParamDescriptor(
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
        elif kind == K_BODY_MODEL:
            yield ParamDescriptor(
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
        elif kind == K_UPLOAD_FILE:
            yield ParamDescriptor(
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
        elif kind == K_QUERY_LIST:
            yield ParamDescriptor(
                name=slot.name,
                wire_name=slot.name,
                location="path" if slot.name in param_names else "query",
                target_type=slot.list_inner,
                is_list=True,
                model=None,
                marker=None,
                is_file=False,
                has_default=slot.has_default,
                default=slot.default,
                is_optional=slot.is_optional,
            )
        elif kind in (K_QUERY, K_PATH):
            yield ParamDescriptor(
                name=slot.name,
                wire_name=slot.name,
                location="path" if (kind == K_PATH or slot.name in param_names) else "query",
                target_type=slot.target_type,
                is_list=False,
                model=None,
                marker=None,
                is_file=False,
                has_default=slot.has_default,
                default=slot.default,
                is_optional=slot.is_optional,
            )
        # Inject-only kinds (request/response/background/scopes/websocket/depends)
        # are not documentable inputs and are intentionally not yielded.
