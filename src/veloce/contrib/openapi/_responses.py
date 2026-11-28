"""Response extraction — success, declared and automatic validation-error responses."""

from __future__ import annotations

from typing import Any

from veloce._constants import MIME_JSON
from veloce.contrib._jsonschema import (
    SchemaRegistry,
    _response_model_to_schema,
)

# Deliberate re-exports (`X as X`); the schema layer itself lives in `_jsonschema`.
from veloce.contrib._jsonschema import _local_def_refs as _local_def_refs
from veloce.contrib._jsonschema import _rewrite_byte_format as _rewrite_byte_format
from veloce.contrib.openapi._security import _dependency_graph_has_validatable
from veloce.status import HTTP_200_OK, HTTP_422_UNPROCESSABLE_ENTITY

# Component names for the auto 422, whose payload the dispatcher emits.
_VALIDATION_ERROR_SCHEMA_NAME = "ValidationError"
_HTTP_VALIDATION_ERROR_SCHEMA_NAME = "HTTPValidationError"

# A placeholder resolved at finalize, so a user-declared 422 referencing its own
# `HTTPValidationError` model is never mistaken for the auto entry.
_AUTO_VALIDATION_ERROR_REF = "#/components/schemas/__veloce_auto_http_validation_error__"

# Reference key (string status code) for the auto-added 422 entry.
_VALIDATION_ERROR_STATUS = str(HTTP_422_UNPROCESSABLE_ENTITY)

# The validation-error envelope, mirroring `RequestValidationError`'s per-error
# items; `loc` is a mixed string/int path, so its items accept either.
_VALIDATION_ERROR_COMPONENT_SCHEMAS: dict[str, dict[str, Any]] = {
    _VALIDATION_ERROR_SCHEMA_NAME: {
        "type": "object",
        "title": _VALIDATION_ERROR_SCHEMA_NAME,
        "properties": {
            "loc": {
                "type": "array",
                "title": "Location",
                "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            },
            "msg": {"type": "string", "title": "Message"},
            "type": {"type": "string", "title": "Error Type"},
        },
        "required": ["loc", "msg", "type"],
    },
    _HTTP_VALIDATION_ERROR_SCHEMA_NAME: {
        "type": "object",
        "title": _HTTP_VALIDATION_ERROR_SCHEMA_NAME,
        "properties": {
            "detail": {
                "type": "array",
                "title": "Detail",
                "items": {"$ref": f"#/components/schemas/{_VALIDATION_ERROR_SCHEMA_NAME}"},
            },
            # The dispatcher emits this alongside `detail`.
            "status_code": {"type": "integer", "title": "Status Code"},
        },
    },
}


def _references_validation_error_schema(operation: dict[str, Any]) -> bool:
    """Return True when `operation`'s 422 entry points at `HTTPValidationError`.

    Used to decide whether the shared validation-error component schemas need to
    be emitted. A user-declared 422 (different `$ref` or inline schema) does not
    trigger registration of the auto component.
    """
    entry = operation.get("responses", {}).get(_VALIDATION_ERROR_STATUS)
    if not isinstance(entry, dict):
        return False
    schema = entry.get("content", {}).get(MIME_JSON, {}).get("schema", {})
    return isinstance(schema, dict) and schema.get("$ref") == _AUTO_VALIDATION_ERROR_REF


def _repoint_validation_error_refs(schema: dict[str, Any], http_name: str) -> None:
    """Resolve the auto-422 placeholder `$ref` to the real envelope component.

    The auto-added 422 responses carry `_AUTO_VALIDATION_ERROR_REF`; this rewrites
    only those to the finalized (collision-free) component name. Because the
    placeholder is internal, a user-declared 422 that references a model named
    `HTTPValidationError` never matches and is left untouched.
    """
    new_ref = f"#/components/schemas/{http_name}"
    groups = [schema.get("paths", {})]
    if "webhooks" in schema:
        groups.append(schema["webhooks"])
    for group in groups:
        for operations in group.values():
            if not isinstance(operations, dict):
                continue
            for operation in operations.values():
                if not isinstance(operation, dict):
                    continue
                entry = operation.get("responses", {}).get(_VALIDATION_ERROR_STATUS)
                if not isinstance(entry, dict):
                    continue
                target = entry.get("content", {}).get(MIME_JSON, {}).get("schema")
                if isinstance(target, dict) and target.get("$ref") == _AUTO_VALIDATION_ERROR_REF:
                    target["$ref"] = new_ref


def _has_validatable_params(inputs: Any, request_body: Any, info: Any) -> bool:
    """Whether this operation can raise `RequestValidationError`, and so advertise a 422.

    Named rather than inlined because the parity test in
    `tests/test_openapi_helper_split.py` has to pass `_extract_responses` the
    same value the orchestrator does. It was a copy of this expression, and the
    copy had already drifted - it omitted the dependency-graph disjunct, so a
    route validated solely through a dependency was compared against the wrong
    expectation, which is the thing that test exists to rule out.
    """
    return (
        bool(inputs.parameters)
        or request_body is not None
        or _dependency_graph_has_validatable(info)
    )


def _extract_responses(
    info: Any, schemas_registry: SchemaRegistry, has_validatable_params: bool
) -> dict[str, dict[str, Any]]:
    """Build the operation `responses` map.

    Seeds with the success response (re-keyed to `info.status_code` when not
    200), attaches `response_model` under the primary status, then merges
    `info.responses`. An `HTTPValidationError` 422 is added when
    `has_validatable_params` is set and the user declared no 422 of their own.
    """
    responses: dict[str, dict[str, Any]] = {
        str(HTTP_200_OK): {"description": info.response_description},
    }
    primary_status = str(info.status_code if info.status_code else HTTP_200_OK)
    if primary_status != str(HTTP_200_OK):
        # Re-key the seeded default to the route's chosen status.
        responses[primary_status] = responses.pop(str(HTTP_200_OK))

    if info.response_model is not None:
        resp_schema = _response_model_to_schema(
            info.response_model,
            schemas_registry,
            info.response_model_include,
            info.response_model_exclude,
        )
        if resp_schema is not None:
            responses[primary_status]["content"] = {MIME_JSON: {"schema": resp_schema}}

    # Skipped when the user already declares a 422 through either route, so a
    # custom shape is preserved; and never added where the resolver cannot raise one.
    _openapi_extra_responses = (info.openapi_extra or {}).get("responses") or {}
    if (
        has_validatable_params
        and _VALIDATION_ERROR_STATUS not in responses
        and _VALIDATION_ERROR_STATUS not in {str(s) for s in (info.responses or {})}
        and _VALIDATION_ERROR_STATUS not in {str(s) for s in _openapi_extra_responses}
    ):
        responses[_VALIDATION_ERROR_STATUS] = {
            "description": "Validation Error",
            "content": {MIME_JSON: {"schema": {"$ref": _AUTO_VALIDATION_ERROR_REF}}},
        }

    for status_code, spec in (info.responses or {}).items():
        key = str(status_code)
        existing = responses.setdefault(key, {})
        if not isinstance(spec, dict):
            continue
        extra_model = spec.get("model")
        if extra_model is not None:
            extra_schema = _response_model_to_schema(extra_model, schemas_registry)
            if extra_schema is not None:
                existing.setdefault("content", {})[MIME_JSON] = {"schema": extra_schema}
        if "description" in spec:
            existing["description"] = spec["description"]
        elif "description" not in existing:
            existing["description"] = ""
        # Allow free-form merging of any other keys (headers, links, etc.).
        for k, v in spec.items():
            if k in ("model", "description"):
                continue
            existing[k] = v

    return responses


# ── Security scheme discovery ──────────────────────────────
