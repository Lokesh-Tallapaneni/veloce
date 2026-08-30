"""OpenAPI 3.1 schema generation — auto-generated from routes."""

from __future__ import annotations

import collections.abc
import contextlib
import copy
import dataclasses
import functools
import inspect
import logging
import warnings
import weakref
from typing import TYPE_CHECKING, Any, get_type_hints

from veloce._constants import MIME_FORM_URLENCODED, MIME_JSON, MIME_MULTIPART_FORM_DATA
from veloce._handler_plan import extract_annotated_marker
from veloce._params import ParamBase
from veloce._protocol_constants import HTTP_METHOD_QUERY
from veloce._route_contract import RouteContract, iter_param_descriptors
from veloce.contrib._jsonschema import (
    SchemaRegistry,
    _apply_marker_constraints,
    _is_model_type,
    _iter_dicts,
    _python_type_to_schema,
    _response_model_to_schema,
    _unique_component_name,
    _warn_schema_fallback,
)

# Two names this module no longer uses itself, kept importable from here
# because they were imported from this module before the schema layer moved
# to `_jsonschema`. The `X as X` spelling marks them as deliberate
# re-exports rather than dead imports.
from veloce.contrib._jsonschema import _local_def_refs as _local_def_refs
from veloce.contrib._jsonschema import _rewrite_byte_format as _rewrite_byte_format
from veloce.dependency import Depends
from veloce.routing.converters import path_param_schemas
from veloce.security.base import SecurityScheme
from veloce.status import HTTP_200_OK, HTTP_422_UNPROCESSABLE_ENTITY

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce

_logger = logging.getLogger(__name__)

# Per-handler memoization of `inspect.signature` + `get_type_hints`. The
# OpenAPI generator visits each handler from four sites (operation
# parameters, webhook bodies, dependency-graph walk, dependency leaves),
# so introspecting once and reusing the result eliminates redundant
# work on every schema rebuild. `WeakKeyDictionary` so test suites and
# hot-reload sessions don't pin handlers for the process lifetime.
_HANDLER_INTRO_CACHE: weakref.WeakKeyDictionary[Any, tuple[Any, dict[str, Any]]] = (
    weakref.WeakKeyDictionary()
)

# ── Introspection / merge helpers ──────────────────────────


def _group_field_schema(model: Any, wire_name: str) -> dict[str, Any] | None:
    """Return one field's declared schema from a grouped model, or `None`.

    `None` when the field resolves to a `$ref` (a nested model), which a
    parameter schema cannot carry - the caller falls back to the annotation.
    That fallback is lossy: the caller rebuilds the schema from the annotation
    alone, so the field's own `ge` / `le` / `title` do not reach the document
    while the resolver goes on enforcing them. A `$ref` is the expected reason
    to take it; an introspection failure is not, and is reported rather than
    left to look like the ordinary case.
    """
    try:
        properties = _grouped_model_properties(model)
    except Exception as exc:
        _warn_schema_fallback(f"grouped field {wire_name!r} on {model!r}", exc)
        return None
    prop = properties.get(wire_name)
    if prop is None or "$ref" in prop:
        return None
    return dict(prop)


@functools.lru_cache(maxsize=256)
def _grouped_model_properties(model: Any) -> dict[str, Any]:
    """`{wire name: schema}` for a grouped model, by alias where one is set."""
    schema = model.model_json_schema(by_alias=True)
    return schema.get("properties") or {}


def _handler_intro(handler: Any) -> tuple[Any, dict[str, Any]]:
    """Return `(signature, hints)` for `handler`, memoized per callable.

    `signature` is `None` when `inspect.signature` cannot introspect
    the handler (built-ins, `functools.partial` chains with non-callable
    `func`). `hints` falls back to `{}` on the same failure modes.
    """
    try:
        cached = _HANDLER_INTRO_CACHE.get(handler)
    except TypeError:
        cached = None
    if cached is not None:
        return cached
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        sig = None
    hints: dict[str, Any] = {}
    if hasattr(handler, "__annotations__"):
        try:
            # `include_extras=True` keeps PEP 593 metadata: an
            # `Annotated[..., Security(...)]` parameter carries its marker there
            # and nowhere else, and stripping it published the route as
            # unauthenticated while the runtime still enforced it.
            hints = get_type_hints(handler, include_extras=True)
        except Exception:
            # `get_type_hints` raises a wide range (NameError on unresolved
            # forward refs, TypeError on bad annotations, recursion errors on
            # cyclic models); schema generation degrades gracefully to no hints
            # rather than failing the whole `/docs` build over one handler.
            hints = {}
    result = (sig, hints)
    with contextlib.suppress(TypeError):
        _HANDLER_INTRO_CACHE[handler] = result
    return result


def _param_marker(param: Any, hints: dict[str, Any]) -> Any:
    """Find the `Depends` / parameter marker for `param`, however it was spelled.

    A marker reaches a handler two ways: as the parameter default
    (`cred = Security(scheme)`) or as PEP 593 metadata
    (`cred: Annotated[object, Security(scheme)]`). Reading only the default
    published an `Annotated`-spelled route as unauthenticated while the runtime
    enforced it - and `Annotated` is this project's documented house style, so
    the recommended form was the broken one.

    Resolution is delegated to the same helper the handler plan uses, so the
    published contract and the enforced one cannot drift apart again.
    """
    default = param.default
    if isinstance(default, (Depends, ParamBase)):
        return default
    marker, _base = extract_annotated_marker(hints.get(param.name, param.annotation))
    return marker if marker is not None else default


def _deep_merge(target: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Recursively merge `overlay` into `target` in place.

    Nested dicts merge key-by-key; any non-dict value (scalar, list)
    in `overlay` replaces the value in `target`.
    """
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


# ── Python type → JSON Schema helpers ──────────────────────


# ── Info / parameter / body / response builders ────────────


def _build_info_object(app: Veloce) -> dict[str, Any]:
    """Return the OpenAPI `info` object assembled from app metadata."""
    # Read directly: `Veloce.__init__` requires both to be non-empty strings, so
    # a fallback here is a second copy of a default that cannot be reached. The
    # two copies had already disagreed - this one said "Veloce API" where the
    # constructor and the MCP server said "Veloce", so one app named itself two
    # things across its two doors.
    info_obj: dict[str, Any] = {"title": app.title, "version": app.version}
    if app.summary:
        info_obj["summary"] = app.summary
    if app.description:
        info_obj["description"] = app.description
    if app.terms_of_service:
        info_obj["termsOfService"] = app.terms_of_service
    if app.contact:
        info_obj["contact"] = app.contact
    if app.license_info:
        info_obj["license"] = app.license_info
    return info_obj


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

    Each field of the returned `_RouteParameters` is documented on the record
    itself; `_extract_request_body` consumes the three body-shaped ones.

    Walks the same `HandlerPlan` the resolver executes (via
    `iter_param_descriptors`), so the documented contract matches the one the
    server enforces. Depends/Security and JSON `Body()` markers are not yielded
    as parameters - they belong to other parts of the operation object.
    """
    parameters: list[dict[str, Any]] = []
    request_body_schema: dict[str, Any] | None = None
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
            # A bare model or `Body()`-wrapped model is the JSON request body. A
            # JSON body is always required there: the resolver 422s on a missing
            # body even for an `Optional` model.
            if d.model is not None:
                request_body_schema = schemas_registry.ref(d.model, mode="validation")
                continue
            # A `Body()` over a non-model still has a documentable shape, and
            # which shape depends on `embed`: an embedded param is one named key
            # of a JSON object body, while a non-embedded one receives the whole
            # body. Both are collected here so the operation documents the body
            # the resolver actually reads.
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
                # A bare `UploadFile`: optional when it carries a default or an
                # `Optional` annotation - the resolver leaves the kwarg unset and
                # the handler default applies, so the field is not required.
                field_required = _is_required(d, None)
                field_alias = d.name
            form_fields.append(_FormField(field_alias, field_schema, field_required, d.is_file))
            continue

        # path / query / header / cookie parameter.
        param_schema: dict[str, Any]
        if d.group_field and (grouped := _group_field_schema(d.model, d.wire_name)) is not None:
            # The field's own declaration owns its constraints; rebuilding the
            # schema from the annotation alone would publish a laxer contract
            # than the resolver enforces.
            param_schema = grouped
        elif d.is_list:
            param_schema = {"type": "array", "items": _python_type_to_schema(d.target_type)}
        else:
            param_schema = _python_type_to_schema(d.target_type)

        if marker is not None:
            _apply_marker_constraints(param_schema, marker)

        # Path parameters document the Python name; every other location honours
        # the alias / hyphenated wire name the resolver actually reads.
        param_alias = d.name if location == "path" else d.wire_name

        # A path parameter is always required - the route cannot match without
        # its segment (OpenAPI 3.1 requires `required: true`). For every other
        # location an `Optional[T]` annotation makes the value omittable even
        # with no default: the resolver binds `None` when it is absent, so the
        # documented contract matches the resolver only when `is_optional`
        # (or a default) relaxes required.
        if location == "path":
            required = True
        elif marker is not None:
            required = _is_required(d, marker)
            if marker.has_default and marker.default is not ...:
                default_val = marker.default
                if isinstance(default_val, (str, int, float, bool, type(None))):
                    param_schema["default"] = default_val
        else:
            required = _is_required(d, None)
            if d.has_default:
                default_val = d.default
                if isinstance(default_val, (str, int, float, bool, type(None))):
                    param_schema["default"] = default_val

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
    return _RouteParameters(parameters, request_body_schema, form_fields, body_fields, scalar_body)


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


def _extract_request_body(
    request_body_schema: dict[str, Any] | None,
    form_fields: list[_FormField],
    body_fields: list[_BodyField] | None = None,
    scalar_body: _ScalarBody | None = None,
) -> dict[str, Any] | None:
    """Build the OpenAPI `requestBody` object, or `None` when no body params exist.

    A JSON Pydantic body takes precedence over form fields, matching the
    monolithic implementation. When only form fields are present, the
    media type is `multipart/form-data` if any field is a file upload
    (OpenAPI 3.1 Sec. 4.8.10.4), otherwise `application/x-www-form-urlencoded`.

    A JSON body is always required - the resolver 422s on a missing body. A form
    body whose every field is optional is omittable, so it is documented
    `required: false` to match the runtime.
    """
    if request_body_schema:
        return {
            "required": True,
            "content": {MIME_JSON: {"schema": request_body_schema}},
        }
    # `Body(embed=True)` params are named keys of one JSON object body. The body
    # is only required when at least one of them is, matching a resolver that
    # accepts an absent body when every field carries a default.
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
    # A non-embedded `Body()` over a non-model receives the whole JSON body, so
    # the body schema is that value's own schema rather than an object wrapper.
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


# Component-schema names for the auto-generated validation-error response. The
# `{"detail": [{"loc", "msg", "type"}, ...], "status_code": 422}` payload these
# describe is what the dispatcher emits for a failed path/query/header/cookie/
# body/form parameter, so the document advertises the body a client receives.
# (`request_validation_exception_handler` is exported for applications that want
# to install it; it is not registered by default and is not this path.)
_VALIDATION_ERROR_SCHEMA_NAME = "ValidationError"
_HTTP_VALIDATION_ERROR_SCHEMA_NAME = "HTTPValidationError"

# The auto-added 422 response points at this internal placeholder ref rather than
# the real component name. At finalize the placeholder is resolved to the actual
# (collision-free) envelope component everywhere it appears. Using a placeholder
# means a user-declared 422 that legitimately references a model named
# `HTTPValidationError` is never mistaken for - or rewritten as - the auto entry.
_AUTO_VALIDATION_ERROR_REF = "#/components/schemas/__veloce_auto_http_validation_error__"

# Reference key (string status code) for the auto-added 422 entry.
_VALIDATION_ERROR_STATUS = str(HTTP_422_UNPROCESSABLE_ENTITY)

# Component schemas for the validation-error envelope, mirroring the per-error
# items `RequestValidationError` carries (`loc` / `msg` / `type`). `loc` is a
# mixed string/int path (`["query", "n"]`, `["body", 0, "name"]`), so its items
# accept either; the wrapper nests an array of these under `detail`.
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
            # The dispatcher emits this alongside `detail`; a schema that omits
            # it describes a body no client actually receives.
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


def _extract_responses(
    info: Any, schemas_registry: SchemaRegistry, has_validatable_params: bool
) -> dict[str, dict[str, Any]]:
    """Build the operation `responses` map.

    Seeds with the success response (re-keyed to `info.status_code` when
    not 200), attaches `response_model` under the primary status, then
    merges entries from `info.responses` - each carrying `model`,
    `description`, or any free-form OpenAPI keys.

    When `has_validatable_params` is set and the user has not already declared
    a 422, an `HTTPValidationError` entry is added: the resolver raises
    `RequestValidationError` (rendered as a 422 `{"detail": [...]}`) whenever a
    request-bound parameter fails validation, so the documented responses match
    what the operation actually returns.
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

    # Auto-add the validation-error response for operations whose request is
    # validated. Skipped when the user already declares a 422 - via `responses=`
    # OR `openapi_extra={"responses": {"422": ...}}` (which is deep-merged onto
    # the operation later) - so a custom 422 shape / media type is preserved
    # rather than overwritten. Operations with no validatable parameter never
    # advertise a 422 the resolver cannot raise.
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


def _scheme_definition(scheme: Any) -> tuple[str, dict[str, Any]] | None:
    """Return `(name, OpenAPI security scheme object)` for a Security() target.

    The scheme describes itself through `SecurityScheme.openapi_scheme`. A
    nine-branch `isinstance` cascade over the built-in classes lived here and
    returned `None` for everything else, so a user's own `SecurityScheme`
    subclass authenticated correctly at runtime and was published as an
    endpoint with no security requirement - a document asserting the route was
    open. Asking the object means a scheme defined outside this package is
    published like a built-in.

    `None` still means "cannot be described", which the caller reports rather
    than passing off as an unguarded route. The name is the scheme's class name
    so repeated registrations share one `components.securitySchemes` entry.
    """
    describe = getattr(scheme, "openapi_scheme", None)
    if describe is None:
        return None
    definition = describe()
    if not definition:
        return None
    return type(scheme).__name__, definition


def _dependency_params(target: Any) -> collections.abc.Iterator[tuple[Any, Any, Any]]:
    """Yield `(parameter, marker, annotation)` for each parameter of `target`.

    The descent step both dependency-graph walkers take: introspect, then read
    each parameter's marker however it was spelled. Written twice, a change to
    how a marker is recognised reaches one walker and not the other - and the
    two answer questions (which security schemes guard this route; does
    anything here consume validated input) that must agree about what the graph
    contains. The annotation is the resolved one, falling back to whatever the
    signature carries. Yields nothing for a target that cannot be introspected.
    """
    sig, hints = _handler_intro(target)
    if sig is None:
        return
    for param in sig.parameters.values():
        yield param, _param_marker(param, hints), hints.get(param.name, param.annotation)


def _collect_security_requirements(
    info: Any, registry: dict[str, dict[str, Any]]
) -> list[dict[str, list[str]]]:
    """Collect one OpenAPI security requirement per reachable `Security()`.

    Walks the route's dependency chain, mutating `registry` to add
    `components.securitySchemes` entries. Returns the operation-level
    `security` list — a sequence of `{schemeName: [scopes]}` dicts, empty
    when no `Security()` is reachable.
    """
    requirements: list[dict[str, list[str]]] = []
    seen: set[int] = set()

    def visit(dep: Any) -> None:
        # `dep` is either a Depends/Security marker, or a callable that
        # might be one of the known scheme classes.
        if isinstance(dep, Depends):
            target = dep.dependency
            scheme_def = _scheme_definition(target)
            if scheme_def is not None:
                name, definition = scheme_def
                registry.setdefault(name, definition)
                # Scopes only matter for OAuth2; Security() carries them,
                # plain Depends doesn't.
                scopes = list(getattr(dep, "scopes", []) or [])
                requirements.append({name: scopes})
                # Don't recurse past a known scheme - its internals are
                # implementation, not policy.
                return
            if isinstance(target, SecurityScheme):
                # It guards the route but cannot say how. Publishing nothing
                # asserts the route is open, which is the one thing that must
                # not happen quietly - a generated client and the Authorize
                # button both read this as "no credential needed".
                warnings.warn(
                    f"{type(target).__name__} guards a route but does not implement "
                    "openapi_scheme(), so the route is published with no security "
                    "requirement - readers of the schema will see it as unauthenticated. "
                    "Return an OpenAPI Security Scheme Object from openapi_scheme().",
                    stacklevel=2,
                )
                return
            # Generic dep - recurse into its handler signature.
            inner = target
        else:
            inner = dep

        if inner is None or id(inner) in seen:
            return
        seen.add(id(inner))

        for _param, default, _annotation in _dependency_params(inner):
            if isinstance(default, Depends):
                visit(default)

    # Route-level dependencies (the `dependencies=[Depends(...)]` kwarg).
    for d in info.dependencies or ():
        visit(d)
    # Plus anything in the handler's own parameter defaults.
    for _param, default, _annotation in _dependency_params(info.handler):
        if isinstance(default, Depends):
            visit(default)
    return requirements


# ── Operation + webhook assembly ───────────────────────────


def _dependency_graph_has_validatable(info: Any) -> bool:
    """Return True if any dependency in the route's graph consumes validated input.

    The resolver raises `RequestValidationError` (422) for a Query/Path/Header/
    Cookie/Form/Body marker or a body-model parameter anywhere in the dependency
    graph - not only at the top level - so the auto-generated 422 response must
    reflect sub-dependency validation too. Known security schemes are not
    request-validated user input, so the walk does not descend into them.
    """
    seen: set[int] = set()

    def visit(dep_callable: Any) -> bool:
        if dep_callable is None or id(dep_callable) in seen:
            return False
        seen.add(id(dep_callable))
        for _param, default, annotation in _dependency_params(dep_callable):
            if isinstance(default, Depends):
                target = default.dependency
                if _scheme_definition(target) is not None:
                    continue
                if visit(target):
                    return True
                continue
            if isinstance(default, ParamBase):
                return True
            if _is_model_type(annotation):
                return True
        return False

    for dep in info.dependencies or []:
        if isinstance(dep, Depends):
            target = dep.dependency
            if _scheme_definition(target) is None and visit(target):
                return True
    return visit(info.handler)


def _operation_base(info: Any, method_lower: str) -> dict[str, Any]:
    """Build the operation fields a path operation and a webhook both carry.

    Shared so a field added to one is not silently missing from the other; the
    caller adds whatever its own kind of operation defines on top.
    """
    # OpenAPI 3.1 Sec. 4.8.10 - operationId must be unique across the document.
    # Explicit override wins; default = `<name>_<method>`. Collisions among the
    # auto-generated form are resolved later in `_disambiguate_operation_ids`.
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
        inputs.request_body_schema, inputs.form_fields, inputs.body_fields, inputs.scalar_body
    )
    if request_body is not None:
        operation["requestBody"] = request_body

    # Walk this route's Security() chain to register OpenAPI security
    # schemes and attach the operation-level `security` requirement.
    security_requirements = _collect_security_requirements(info, security_schemes_registry)
    if security_requirements:
        operation["security"] = security_requirements

    # An operation can return a 422 only when it carries something the resolver
    # validates: a path/query/header/cookie parameter, a JSON body, a form field,
    # or any validated input inside a `Depends(...)` sub-dependency. A handler
    # with none of these never raises `RequestValidationError`, so it must not
    # advertise a 422.
    has_validatable_params = (
        bool(inputs.parameters)
        or request_body is not None
        or _dependency_graph_has_validatable(info)
    )
    operation["responses"] = _extract_responses(info, schemas_registry, has_validatable_params)
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

    Empty when no webhooks router exists or it has no routes. Each entry
    is keyed by event name (the path on `@app.webhooks.post`) and carries
    one operation per HTTP method registered. Each webhook's auto-generated
    operationId is appended to `auto_ops` so it flows through the same
    document-wide disambiguation pass as normal routes; two webhooks sharing a
    handler name (or a webhook colliding with a route) would otherwise emit a
    duplicate operationId, which is invalid for code generation (OpenAPI 3.1
    Sec. 4.8.10). A webhook that pins its own `operation_id` goes to
    `explicit_ops` instead, so the pass reserves it rather than renaming it.

    `app.webhooks` is an ordinary `Blueprint` and each entry an ordinary
    `RouteInfo`, so a webhook accepts every operation keyword a route does;
    the shared `_operation_base` is what keeps them describing the same fields.
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
            # The disambiguator keys collisions on a deterministic identifier; a
            # webhook has no URL path, so its event name stands in as the
            # suffix source if a collision must be resolved.
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
    Two handlers sharing a function name on different paths would otherwise emit
    the same id and break client code generation. Each duplicate after the
    first keeps a deterministic path-derived suffix; a single aggregated WARNING
    lists every collision and its resolution.

    User-pinned operationIds (`explicit_ops`) are reserved up front and never
    rewritten: an auto id clashing with a pinned id (or with another auto id) is
    suffixed instead. Two identical *explicit* ids are a user error the document
    cannot silently fix by renaming a pinned id, so they are surfaced via a
    WARNING rather than left to ship duplicated.
    """
    explicit_ops = explicit_ops or []

    # Reserve every explicit id first so auto ids are disambiguated against them.
    # `assigned` accumulates ids already taken across the whole document, so the
    # suffix search below never collides with a pinned id or an earlier auto id.
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
        # An auto id is left on its bare form only when nothing else (no pinned
        # id, no earlier auto group) already claimed it. Otherwise every member
        # of the group, including the first, is suffixed from its path so the
        # assignment is stable across regenerations of the document.
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


def _validate_document(schema: dict[str, Any]) -> None:
    """Lightweight structural pass over the assembled OpenAPI document.

    Opt-in (gated on `app.validate_openapi`, defaulting to `app.debug`) so the
    common production build pays nothing. Catches malformed entries from the
    free-form `info.responses` merge, a bad `openapi_extra` `_deep_merge`, or a
    dangling `$ref` before the document reaches Swagger UI, raising a
    `ValueError` that names the offending path and method.
    """
    # `info.title` and `info.version` are REQUIRED and are strings - OpenAPI 3.1
    # Sec. 4.8.2. The pass checked `$ref`s and container shapes and never these,
    # so `validate_openapi=True` accepted a document it was asked to reject.
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


# `app` is `Any` rather than `Veloce`: `OpenAPIMixin.openapi()` calls this with
# `self`, which only the assembled subclass declares to be a `Veloce`. Every
# helper it hands the app to takes the concrete type, so the check is restored
# one frame in.
def get_openapi_schema(app: Any) -> dict[str, Any]:
    """Generate OpenAPI 3.1 schema from the app's registered routes."""
    schema: dict[str, Any] = {
        # `app.openapi_version` is documented as "the spec version string emitted
        # in the document", and was emitted nowhere: setting it changed the
        # attribute and not the document.
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

    # Operations whose operationId was auto-generated (no explicit override),
    # recorded so a deterministic suffix can resolve duplicates afterwards.
    auto_ops: list[tuple[dict[str, Any], str, str]] = []
    # Operations carrying a user-pinned `operation_id`. These are reserved
    # during disambiguation so an auto id colliding with a pinned id suffixes
    # the AUTO one (a user's explicit id is never rewritten), and two identical
    # pinned ids are detected and warned about (a user error the document can't
    # silently fix by renaming).
    explicit_ops: list[tuple[dict[str, Any], str, str]] = []

    # Set once any operation auto-adds the validation-error response, so the
    # `HTTPValidationError` / `ValidationError` component schemas are emitted only
    # when something actually references them.
    needs_validation_error_schema = False

    for method, path, info in app.iter_routes():
        # OpenAPI 3.1's Path Item Object has no `query` field, so a QUERY route
        # cannot be represented without emitting an invalid operation. Omit it
        # rather than produce an invalid document (native support awaits the
        # OpenAPI 3.2 `query` operation - RFC 10008).
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

    # Webhook operations are appended to `auto_ops` so the disambiguation pass
    # below dedupes operationIds across BOTH routes and webhooks deterministically
    # (routes first in collection order, then webhooks in walker order).
    webhook_items = _walk_webhooks(app, schemas_registry, auto_ops, explicit_ops)
    if webhook_items:
        schema["webhooks"] = webhook_items

    if app.disambiguate_operation_ids:
        _disambiguate_operation_ids(auto_ops, explicit_ops)

    components_schemas = schemas_registry.finalize(schema)
    # Add the validation-error component schemas only when an operation referenced
    # them. If a user model already occupies `HTTPValidationError` / `ValidationError`,
    # register the auto envelope under collision-free names and repoint the 422
    # `$ref`s, so the documented validation-error payload is never silently bound to
    # an unrelated user model.
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
        # Operations reference the auto-422 via an internal placeholder; resolve
        # it to the finalized component name everywhere (always, since the
        # placeholder is never a real component).
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
