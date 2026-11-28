"""The document — validation and the top-level `get_openapi_schema` entry point."""

from __future__ import annotations

import copy
import warnings
from typing import Any

from veloce._protocol_constants import HTTP_METHOD_QUERY
from veloce._warnings import _RegistrationWarning
from veloce.contrib._jsonschema import (
    SchemaRegistry,
    _iter_dicts,
    _unique_component_name,
)

# Deliberate re-exports (`X as X`); the schema layer itself lives in `_jsonschema`.
from veloce.contrib._jsonschema import _local_def_refs as _local_def_refs
from veloce.contrib._jsonschema import _rewrite_byte_format as _rewrite_byte_format
from veloce.contrib.openapi._introspection import _build_info_object
from veloce.contrib.openapi._operations import (
    _build_operation,
    _disambiguate_operation_ids,
    _walk_webhooks,
)
from veloce.contrib.openapi._responses import (
    _HTTP_VALIDATION_ERROR_SCHEMA_NAME,
    _VALIDATION_ERROR_COMPONENT_SCHEMAS,
    _VALIDATION_ERROR_SCHEMA_NAME,
    _references_validation_error_schema,
    _repoint_validation_error_refs,
)


def _validate_document(schema: dict[str, Any]) -> None:
    """Lightweight structural pass over the assembled OpenAPI document.

    Opt-in (gated on `app.validate_openapi`, defaulting to `app.debug`) so the
    common production build pays nothing. Catches malformed entries from the
    free-form `info.responses` merge, a bad `openapi_extra` `_deep_merge`, or a
    dangling `$ref` before the document reaches Swagger UI, raising a
    `ValueError` that names the offending path and method.
    """
    # `info.title` and `info.version` are REQUIRED and are strings - OpenAPI 3.1
    # Sec. 4.8.2.
    info = schema.get("info")
    if not isinstance(info, dict):
        raise ValueError("OpenAPI document `info` must be an object")
    for field in ("title", "version"):
        value = info.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"OpenAPI document `info.{field}` must be a non-empty string, got {value!r}"
            )

    schemas = schema.get("components", {}).get("schemas", {})
    paths = schema.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI document `paths` must be an object")

    known_refs = {f"#/components/schemas/{name}" for name in schemas}

    def check_refs(node: Any, where: str) -> None:
        for mapping in _iter_dicts(node):
            ref = mapping.get("$ref")
            if (
                isinstance(ref, str)
                and ref.startswith("#/components/schemas/")
                and ref not in known_refs
            ):
                raise ValueError(f"{where}: unresolved schema $ref {ref!r}")

    for path, methods in paths.items():
        if not isinstance(methods, dict):
            raise ValueError(f"path {path!r}: operations container must be an object")
        for method, operation in methods.items():
            where = f"{method.upper()} {path}"
            if not isinstance(operation, dict):
                raise ValueError(f"{where}: operation must be an object")
            responses = operation.get("responses")
            if not isinstance(responses, dict) or not responses:
                raise ValueError(f"{where}: operation must declare at least one response")
            for param in operation.get("parameters", []) or []:
                if not isinstance(param, dict) or "name" not in param or "in" not in param:
                    raise ValueError(f"{where}: every parameter needs `name` and `in`")
            check_refs(operation, where)

    check_refs(schemas, "components.schemas")


# ── Public API ─────────────────────────────────────────────


# `app` is `Any` because `OpenAPIMixin.openapi()` calls this with `self`.
def get_openapi_schema(app: Any) -> dict[str, Any]:
    """Generate OpenAPI 3.1 schema from the app's registered routes."""
    if not app.openapi_version.startswith("3.1"):
        # The schemas are JSON Schema 2020-12, the dialect OpenAPI 3.1 adopts, so
        # naming a 3.0 version here would describe a document 3.0 cannot validate.
        warnings.warn(
            f"openapi_version is {app.openapi_version!r}, and the document's schemas are "
            "written in the JSON Schema 2020-12 dialect that OpenAPI 3.1 adopts; a 3.0 "
            "consumer may reject it. The attribute sets the version string only.",
            _RegistrationWarning,
            stacklevel=2,
        )
    schema: dict[str, Any] = {
        # `app.openapi_version` is the spec version string emitted in the document.
        "openapi": app.openapi_version,
        "info": _build_info_object(app),
        "paths": {},
        "components": {"schemas": {}},
    }

    if app.servers:
        schema["servers"] = app.servers
    if app.openapi_tags:
        schema["tags"] = app.openapi_tags
    # OpenAPI 3.1 Sec. 4.8.11 - top-level `externalDocs` object.
    if app.openapi_external_docs:
        schema["externalDocs"] = app.openapi_external_docs

    schemas_registry = SchemaRegistry(separate_input_output=app.separate_input_output_schemas)
    security_schemes_registry: dict[str, dict[str, Any]] = {}

    # Auto-generated operationIds, recorded so a suffix can resolve duplicates.
    auto_ops: list[tuple[dict[str, Any], str, str]] = []
    # User-pinned ids, reserved during disambiguation so a colliding auto id is the
    # one suffixed; two identical pinned ids are warned about instead.
    explicit_ops: list[tuple[dict[str, Any], str, str]] = []

    # Set once anything auto-adds a 422, so the components are emitted only then.
    needs_validation_error_schema = False

    for method, path, info in app.iter_routes():
        # OpenAPI 3.1's Path Item Object has no `query` field, so a QUERY route is
        # omitted rather than emitted as an invalid operation (3.2 adds it).
        if method == HTTP_METHOD_QUERY:
            continue
        method_lower = method.lower()
        if path not in schema["paths"]:
            schema["paths"][path] = {}
        operation = _build_operation(
            info, method_lower, schemas_registry, security_schemes_registry
        )
        if _references_validation_error_schema(operation):
            needs_validation_error_schema = True
        schema["paths"][path][method_lower] = operation
        if info.operation_id:
            explicit_ops.append((operation, path, method_lower))
        else:
            auto_ops.append((operation, path, method_lower))

    # Appended to `auto_ops` so disambiguation spans routes and webhooks alike.
    webhook_items = _walk_webhooks(app, schemas_registry, auto_ops, explicit_ops)
    if webhook_items:
        schema["webhooks"] = webhook_items

    if app.disambiguate_operation_ids:
        _disambiguate_operation_ids(auto_ops, explicit_ops)

    components_schemas = schemas_registry.finalize(schema)
    # Added only when an operation referenced them, and under collision-free names
    # when a user model holds the default ones, so no 422 binds to an unrelated model.
    if needs_validation_error_schema:
        val_name = _unique_component_name(_VALIDATION_ERROR_SCHEMA_NAME, components_schemas)
        http_name = _unique_component_name(_HTTP_VALIDATION_ERROR_SCHEMA_NAME, components_schemas)
        val_body = copy.deepcopy(_VALIDATION_ERROR_COMPONENT_SCHEMAS[_VALIDATION_ERROR_SCHEMA_NAME])
        val_body["title"] = val_name
        http_body = copy.deepcopy(
            _VALIDATION_ERROR_COMPONENT_SCHEMAS[_HTTP_VALIDATION_ERROR_SCHEMA_NAME]
        )
        http_body["title"] = http_name
        http_body["properties"]["detail"]["items"]["$ref"] = f"#/components/schemas/{val_name}"
        components_schemas[val_name] = val_body
        components_schemas[http_name] = http_body
        # Always resolved, since the placeholder is never itself a real component.
        _repoint_validation_error_refs(schema, http_name)
    if components_schemas:
        schema["components"]["schemas"] = components_schemas
    if security_schemes_registry:
        schema["components"]["securitySchemes"] = security_schemes_registry

    validate = app.validate_openapi
    if validate is None:
        validate = bool(app.debug)
    if validate:
        _validate_document(schema)

    return schema
