"""Request-body extraction — JSON, form and multipart bodies of one route."""

from __future__ import annotations

from typing import Any

from veloce._constants import MIME_FORM_URLENCODED, MIME_JSON, MIME_MULTIPART_FORM_DATA

# Deliberate re-exports (`X as X`); the schema layer itself lives in `_jsonschema`.
from veloce.contrib._jsonschema import _local_def_refs as _local_def_refs
from veloce.contrib._jsonschema import _rewrite_byte_format as _rewrite_byte_format
from veloce.contrib.openapi._parameters import _BodyField, _FormField, _ScalarBody


def _extract_request_body(
    request_body_schema: dict[str, Any] | None,
    request_body_required: bool,
    form_fields: list[_FormField],
    body_fields: list[_BodyField] | None = None,
    scalar_body: _ScalarBody | None = None,
) -> dict[str, Any] | None:
    """Build the OpenAPI `requestBody` object, or `None` when no body params exist.

    A JSON body takes precedence over form fields. For form fields the media
    type is `multipart/form-data` when any field is a file upload (OpenAPI 3.1
    Sec. 4.8.10.4), else `application/x-www-form-urlencoded`. A JSON body is
    required unless a default also makes it omittable; a form body whose every
    field is optional is not required either way.
    """
    if request_body_schema:
        return {
            "required": request_body_required,
            "content": {MIME_JSON: {"schema": request_body_schema}},
        }
    # `Body(embed=True)` keys make the body required only when one of them is.
    if body_fields:
        embed_properties: dict[str, Any] = {}
        embed_required: list[str] = []
        for body_field in body_fields:
            bname = body_field.alias
            bschema = body_field.schema
            breq = body_field.required
            embed_properties[bname] = bschema
            if breq:
                embed_required.append(bname)
        embed_schema: dict[str, Any] = {"type": "object", "properties": embed_properties}
        if embed_required:
            embed_schema["required"] = embed_required
        return {
            "required": bool(embed_required),
            "content": {MIME_JSON: {"schema": embed_schema}},
        }
    # A non-embedded `Body()` receives the whole JSON body, not a wrapper object.
    if scalar_body is not None:
        schema, required = scalar_body.schema, scalar_body.required
        return {"required": required, "content": {MIME_JSON: {"schema": schema}}}
    if not form_fields:
        return None
    has_file = any(form_field.is_file for form_field in form_fields)
    media_type = MIME_MULTIPART_FORM_DATA if has_file else MIME_FORM_URLENCODED
    properties: dict[str, Any] = {}
    required_fields: list[str] = []
    for form_field in form_fields:
        properties[form_field.alias] = form_field.schema
        if form_field.required:
            required_fields.append(form_field.alias)
    body_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required_fields:
        body_schema["required"] = required_fields
    return {
        "required": bool(required_fields),
        "content": {media_type: {"schema": body_schema}},
    }
