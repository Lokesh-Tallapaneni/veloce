"""Parameter extraction — path, query, header, cookie and form inputs of one route."""

from __future__ import annotations

import dataclasses
from typing import Any

from veloce._route_contract import RouteContract, iter_param_descriptors
from veloce.contrib._jsonschema import (
    _NO_DEFAULT,
    SchemaRegistry,
    _apply_marker_constraints,
    _publishable_default,
    _python_type_to_schema,
)

# Deliberate re-exports (`X as X`); the schema layer itself lives in `_jsonschema`.
from veloce.contrib._jsonschema import _local_def_refs as _local_def_refs
from veloce.contrib._jsonschema import _rewrite_byte_format as _rewrite_byte_format
from veloce.contrib.openapi._introspection import _group_field_schema
from veloce.routing.converters import path_param_schemas


@dataclasses.dataclass(frozen=True, slots=True)
class _FormField:
    """One `Form()` / `File()` parameter, as the request body will describe it."""

    alias: str
    schema: dict[str, Any]
    required: bool
    is_file: bool


@dataclasses.dataclass(frozen=True, slots=True)
class _BodyField:
    """One `Body(embed=True)` parameter - a named key of the JSON object body."""

    alias: str
    schema: dict[str, Any]
    required: bool


@dataclasses.dataclass(frozen=True, slots=True)
class _ScalarBody:
    """A non-embedded `Body()` over a non-model: the whole JSON body."""

    schema: dict[str, Any]
    required: bool


@dataclasses.dataclass(frozen=True, slots=True)
class _RouteParameters:
    """What lowering a route's handler plan says about its inputs.

    The fields were a 5-tuple whose meanings lived in prose, unpacked
    positionally at the single call site - so `form_fields[0][3]` was `is_file`
    only if you had read the docstring. Naming them puts that in the type.
    """

    parameters: list[dict[str, Any]]
    request_body_schema: dict[str, Any] | None
    request_body_required: bool
    form_fields: list[_FormField]
    body_fields: list[_BodyField]
    scalar_body: _ScalarBody | None


def _is_required(d: Any, marker: Any) -> bool:
    """Whether the parameter `d` (optionally carrying `marker`) must be supplied.

    A default relaxes required, and so does an `Optional[T]` annotation: the
    resolver binds `None` when the value is absent, so a document that called it
    required would contradict what the resolver accepts. Where a marker is
    present its default wins - the marker is the declaration the author wrote.
    """
    carrier = marker if marker is not None else d
    return not (carrier.has_default or d.is_optional)


def _extract_parameters(info: Any, schemas_registry: SchemaRegistry) -> _RouteParameters:
    """Classify every parameter by lowering the route's handler plan.

    Walks the same `HandlerPlan` the resolver executes, so the documented
    contract matches the enforced one. Depends/Security and JSON `Body()`
    markers are not parameters - they belong elsewhere in the operation object.
    """
    parameters: list[dict[str, Any]] = []
    request_body_schema: dict[str, Any] | None = None
    request_body_required = True
    form_fields: list[_FormField] = []
    body_fields: list[_BodyField] = []
    scalar_body: _ScalarBody | None = None

    for d in iter_param_descriptors(RouteContract.from_route_info(info)):
        marker = d.marker
        # `include_in_schema=False` - resolved at runtime but omitted from docs.
        if marker is not None and not getattr(marker, "include_in_schema", True):
            continue

        location = d.location

        if location == "body":
            if d.model is not None:
                request_body_schema = schemas_registry.ref(d.model, mode="validation")
                # A bare body model is required unless a default also makes it
                # omittable; `Optional` typing alone still demands a document.
                # A `Body(...)` marker's own default tracking is unaffected.
                request_body_required = (
                    not (d.has_default and d.is_optional)
                    if marker is None
                    else _is_required(d, marker)
                )
                continue
            # A `Body()` non-model is one named key when embedded, else the body.
            body_schema: dict[str, Any] = _python_type_to_schema(d.target_type)
            if d.is_list:
                body_schema = {"type": "array", "items": body_schema}
            if marker is not None:
                _apply_marker_constraints(body_schema, marker)
                body_required = _is_required(d, marker)
                body_alias = marker.alias or d.name
                embedded = bool(getattr(marker, "embed", False))
            else:
                body_required = _is_required(d, None)
                body_alias = d.name
                embedded = False
            if embedded:
                body_fields.append(_BodyField(body_alias, body_schema, body_required))
            elif scalar_body is None:
                scalar_body = _ScalarBody(body_schema, body_required)
            continue

        if location == "form":
            if d.is_file:
                field_schema: dict[str, Any] = {"type": "string", "format": "binary"}
            else:
                field_schema = _python_type_to_schema(d.target_type)
            if marker is not None:
                if marker.description:
                    field_schema["description"] = marker.description
                if getattr(marker, "title", None):
                    field_schema["title"] = marker.title
                field_required = _is_required(d, marker)
                field_alias = marker.alias or d.name
            else:
                # A bare `UploadFile` is optional given a default or `Optional`.
                field_required = _is_required(d, None)
                field_alias = d.name
            form_fields.append(_FormField(field_alias, field_schema, field_required, d.is_file))
            continue

        # path / query / header / cookie parameter.
        param_schema: dict[str, Any]
        if d.group_field and (grouped := _group_field_schema(d.model, d.wire_name)) is not None:
            # The field's declaration owns its constraints; the annotation is laxer.
            param_schema = grouped
        elif d.is_list:
            param_schema = {"type": "array", "items": _python_type_to_schema(d.target_type)}
        else:
            param_schema = _python_type_to_schema(d.target_type)

        if marker is not None:
            _apply_marker_constraints(param_schema, marker)

        # Path parameters document the Python name, every other the wire name.
        param_alias = d.name if location == "path" else d.wire_name

        # A path parameter is always required (OpenAPI 3.1); elsewhere `Optional[T]`
        # omits it with no default, since the resolver binds `None` when absent.
        if location == "path":
            required = True
        elif marker is not None:
            required = _is_required(d, marker)
            if marker.has_default:
                _default = _publishable_default(marker.default, publish_null=False)
                if _default is not _NO_DEFAULT:
                    param_schema["default"] = _default
        else:
            required = _is_required(d, None)
            if d.has_default:
                _default = _publishable_default(d.default, publish_null=False)
                if _default is not _NO_DEFAULT:
                    param_schema["default"] = _default

        param_info: dict[str, Any] = {
            "name": param_alias,
            "in": location,
            "required": required,
            "schema": param_schema,
        }

        # OpenAPI 3.1 Sec. 4.8.12.1 - array-valued query parameters default
        # to `style: form`, `explode: true` (one `?k=v1&k=v2` per item).
        if location == "query" and param_schema.get("type") == "array":
            param_info["style"] = "form"
            param_info["explode"] = True

        if marker is not None and marker.deprecated:
            param_info["deprecated"] = True

        parameters.append(param_info)

    _declare_undocumented_path_params(info, parameters)
    return _RouteParameters(
        parameters,
        request_body_schema,
        request_body_required,
        form_fields,
        body_fields,
        scalar_body,
    )


def _declare_undocumented_path_params(info: Any, parameters: list[dict[str, Any]]) -> None:
    """Document a path parameter no handler parameter declares.

    A route's path parameters are part of its contract whether or not the
    signature names one: a dependency reading `request.path_params` consumes the
    same segment. Leaving it out also makes the document invalid - OpenAPI 3.1
    requires every template expression in the path to have a `path` parameter -
    so a caller reading the schema could not know to supply it at all.
    """
    template = info.path_template
    if not template:
        return
    declared = {p["name"] for p in parameters if p["in"] == "path"}
    for name, schema in path_param_schemas(template).items():
        if name in declared:
            continue
        parameters.append({"name": name, "in": "path", "required": True, "schema": schema})
