"""Operation assembly — one operation object per route, webhooks and operation ids."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from veloce._constants import MIME_JSON
from veloce.contrib._jsonschema import (
    SchemaRegistry,
    _is_model_type,
)

# Deliberate re-exports (`X as X`); the schema layer itself lives in `_jsonschema`.
from veloce.contrib._jsonschema import _local_def_refs as _local_def_refs
from veloce.contrib._jsonschema import _rewrite_byte_format as _rewrite_byte_format

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce

from veloce.contrib.openapi._introspection import _deep_merge, _handler_intro
from veloce.contrib.openapi._parameters import _extract_parameters
from veloce.contrib.openapi._request_body import _extract_request_body
from veloce.contrib.openapi._responses import _extract_responses, _has_validatable_params
from veloce.contrib.openapi._security import _collect_security_requirements

_logger = logging.getLogger(__name__)


def _operation_base(info: Any, method_lower: str) -> dict[str, Any]:
    """Build the operation fields a path operation and a webhook both carry.

    Shared so a field added to one is not silently missing from the other; the
    caller adds whatever its own kind of operation defines on top.
    """
    # OpenAPI 3.1 Sec. 4.8.10 - operationId must be unique across the document.
    # Explicit override wins; auto collisions resolve in `_disambiguate_operation_ids`.
    op_id = info.operation_id if info.operation_id else f"{info.name}_{method_lower}"
    operation: dict[str, Any] = {
        "summary": info.summary or info.name,
        "operationId": op_id,
        "responses": {"200": {"description": info.response_description}},
    }
    if info.description:
        operation["description"] = info.description
    if info.tags:
        operation["tags"] = info.tags
    if info.deprecated:
        operation["deprecated"] = True
    return operation


def _apply_openapi_extra(operation: dict[str, Any], info: Any) -> None:
    """Deep-merge the author's `openapi_extra` over a finished operation.

    Nested dicts merge key-by-key; scalars and lists in `openapi_extra` overwrite.
    """
    extra = info.openapi_extra
    if extra:
        _deep_merge(operation, extra)


def _build_operation(
    info: Any,
    method_lower: str,
    schemas_registry: SchemaRegistry,
    security_schemes_registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Assemble one OpenAPI operation object for a single route entry."""
    operation = _operation_base(info, method_lower)
    # OpenAPI 3.1 Sec. 4.8.8 - route-level `callbacks` map emitted verbatim.
    if info.callbacks:
        operation["callbacks"] = info.callbacks

    inputs = _extract_parameters(info, schemas_registry)
    if inputs.parameters:
        operation["parameters"] = inputs.parameters

    request_body = _extract_request_body(
        inputs.request_body_schema,
        inputs.request_body_required,
        inputs.form_fields,
        inputs.body_fields,
        inputs.scalar_body,
    )
    if request_body is not None:
        operation["requestBody"] = request_body

    # Registers the schemes and attaches the operation-level `security`.
    security_requirements = _collect_security_requirements(info, security_schemes_registry)
    if security_requirements:
        operation["security"] = security_requirements

    # Only an operation carrying validated input, `Depends` included, can 422.
    operation["responses"] = _extract_responses(
        info, schemas_registry, _has_validatable_params(inputs, request_body, info)
    )
    _apply_openapi_extra(operation, info)
    return operation


def _webhook_request_body(handler: Any, registry: SchemaRegistry) -> dict[str, Any] | None:
    """Return the OpenAPI schema for a webhook handler's Pydantic body param.

    A webhook handler documents the payload an external caller will
    POST; the first BaseModel-typed parameter is treated as that body.
    """
    sig, hints = _handler_intro(handler)
    if sig is None:
        return None
    for pname in sig.parameters:
        if pname in ("self", "request"):
            continue
        annotation = hints.get(pname)
        if annotation and _is_model_type(annotation):
            return registry.ref(annotation, mode="validation")
    return None


def _walk_webhooks(
    app: Veloce,
    schemas_registry: SchemaRegistry,
    auto_ops: list[tuple[dict[str, Any], str, str]],
    explicit_ops: list[tuple[dict[str, Any], str, str]] | None = None,
) -> dict[str, Any]:
    """Return the OpenAPI 3.1 `webhooks` map from `app.webhooks`.

    Each entry is keyed by event name and carries one operation per registered
    method. An auto-generated operationId joins `auto_ops` so it flows through
    the document-wide disambiguation pass (OpenAPI 3.1 Sec. 4.8.10 requires
    uniqueness); a pinned one goes to `explicit_ops` and is reserved instead.
    """
    webhook_items: dict[str, Any] = {}
    webhooks_router = app.webhooks
    walker = getattr(webhooks_router, "_walk_routes", None) if webhooks_router else None
    if walker is None:
        return webhook_items
    for wpath, wmethods, winfo in walker():
        event = wpath.strip("/") or wpath
        for wmethod in wmethods:
            method_lower = wmethod.lower()
            op = _operation_base(winfo, method_lower)
            # A webhook has no URL path, so its event name is the suffix source.
            if winfo.operation_id and explicit_ops is not None:
                explicit_ops.append((op, event, method_lower))
            else:
                auto_ops.append((op, event, method_lower))
            body = _webhook_request_body(winfo.handler, schemas_registry)
            if body is not None:
                op["requestBody"] = {
                    "required": True,
                    "content": {MIME_JSON: {"schema": body}},
                }
            _apply_openapi_extra(op, winfo)
            webhook_items.setdefault(event, {})[method_lower] = op
    return webhook_items


def _disambiguate_operation_ids(
    auto_ops: list[tuple[dict[str, Any], str, str]],
    explicit_ops: list[tuple[dict[str, Any], str, str]] | None = None,
) -> None:
    """Make every auto-generated operationId unique in place.

    OpenAPI 3.1 Sec. 4.8.10 requires `operationId` unique across the document.
    Each duplicate after the first takes a deterministic path-derived suffix,
    aggregated into one WARNING. User-pinned ids (`explicit_ops`) are reserved
    up front and never rewritten; two identical pinned ids are warned about.
    """
    explicit_ops = explicit_ops or []

    # Explicit ids are reserved first, so the suffix search never re-collides.
    assigned: set[str] = set()
    explicit_seen: dict[str, tuple[str, str]] = {}
    explicit_dupes: list[str] = []
    for op, path, method in explicit_ops:
        op_id = op["operationId"]
        if op_id in explicit_seen:
            first_path, first_method = explicit_seen[op_id]
            explicit_dupes.append(
                f"{op_id} ({first_method.upper()} {first_path} and {method.upper()} {path})"
            )
        else:
            explicit_seen[op_id] = (path, method)
            assigned.add(op_id)

    by_id: dict[str, list[tuple[dict[str, Any], str, str]]] = {}
    for op, path, method in auto_ops:
        by_id.setdefault(op["operationId"], []).append((op, path, method))

    resolutions: list[str] = []
    for op_id, group in by_id.items():
        # Otherwise every member is suffixed from its path, stable across rebuilds.
        free_first = op_id not in assigned
        for index, (op, path, method) in enumerate(group):
            if index == 0 and free_first:
                assigned.add(op_id)
                continue
            suffix = "_".join(seg for seg in path.split("/") if seg) or "root"
            candidate = f"{op_id}__{suffix}"
            tail = 1
            while candidate in assigned:
                candidate = f"{op_id}__{suffix}_{tail}"
                tail += 1
            assigned.add(candidate)
            op["operationId"] = candidate
            resolutions.append(f"{op_id} -> {candidate} ({method.upper()} {path})")

    if resolutions:
        _logger.warning(
            "Duplicate OpenAPI operationId(s) auto-disambiguated; pin "
            "`operation_id=` on the affected routes to silence this. "
            "Resolutions: %s",
            "; ".join(resolutions),
        )

    if explicit_dupes:
        _logger.warning(
            "Duplicate explicit OpenAPI operationId(s) - these violate OpenAPI "
            "3.1 Sec. 4.8.10 and cannot be auto-resolved without overriding a "
            "pinned `operation_id=`; rename one of each pair: %s",
            "; ".join(explicit_dupes),
        )
