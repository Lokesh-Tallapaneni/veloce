"""OpenAPI 3.1 schema generation and Swagger UI — auto-generated from routes."""

from __future__ import annotations

import contextlib
import datetime as _datetime
import enum as _enum
import html as _html
import inspect
import logging
import types as _types
import uuid as _uuid
import weakref
from decimal import Decimal
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

import orjson
from pydantic import BaseModel

from veloce.dependency import Depends
from veloce.http.response import HTMLResponse, JSONResponse
from veloce.routing.params import Body as BodyParam
from veloce.routing.params import Cookie as CookieParam
from veloce.routing.params import File as FileParam
from veloce.routing.params import Form as FormParam
from veloce.routing.params import Header as HeaderParam
from veloce.routing.params import ParamBase
from veloce.routing.params import Path as PathParam

_logger = logging.getLogger("veloce.openapi")

# Per-handler memoization of `inspect.signature` + `get_type_hints`. The
# OpenAPI generator visits each handler from four sites (operation
# parameters, webhook bodies, dependency-graph walk, dependency leaves),
# so introspecting once and reusing the result eliminates redundant
# work on every schema rebuild. `WeakKeyDictionary` so test suites and
# hot-reload sessions don't pin handlers for the process lifetime.
_HANDLER_INTRO_CACHE: weakref.WeakKeyDictionary[Any, tuple[Any, dict[str, Any]]] = (
    weakref.WeakKeyDictionary()
)


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
            hints = get_type_hints(handler)
        except Exception:
            hints = {}
    result = (sig, hints)
    with contextlib.suppress(TypeError):
        _HANDLER_INTRO_CACHE[handler] = result
    return result


def _deep_merge(target: dict, overlay: dict) -> None:
    """Recursively merge `overlay` into `target` in place.

    Nested dicts merge key-by-key; any non-dict value (scalar, list)
    in `overlay` replaces the value in `target`.
    """
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _build_info_object(app: Any) -> dict[str, Any]:
    """Return the OpenAPI `info` object assembled from app metadata."""
    info_obj: dict[str, Any] = {
        "title": getattr(app, "title", "Veloce API"),
        "version": getattr(app, "version", "0.1.0"),
    }
    if getattr(app, "summary", None):
        info_obj["summary"] = app.summary
    if getattr(app, "description", ""):
        info_obj["description"] = app.description
    if getattr(app, "terms_of_service", None):
        info_obj["termsOfService"] = app.terms_of_service
    if getattr(app, "contact", None):
        info_obj["contact"] = app.contact
    if getattr(app, "license_info", None):
        info_obj["license"] = app.license_info
    return info_obj


def _apply_marker_constraints(param_schema: dict[str, Any], marker: Any) -> None:
    """Copy validation / metadata keywords from a `ParamBase` marker onto `param_schema`."""
    if getattr(marker, "title", None):
        param_schema["title"] = marker.title
    if marker.description:
        param_schema["description"] = marker.description
    if marker.ge is not None:
        param_schema["minimum"] = marker.ge
    if marker.le is not None:
        param_schema["maximum"] = marker.le
    # OpenAPI 3.1 / JSON Schema 2020-12: gt/lt map to the
    # numeric `exclusiveMinimum` / `exclusiveMaximum`.
    if marker.gt is not None:
        param_schema["exclusiveMinimum"] = marker.gt
    if marker.lt is not None:
        param_schema["exclusiveMaximum"] = marker.lt
    if marker.min_length is not None:
        param_schema["minLength"] = marker.min_length
    if marker.max_length is not None:
        param_schema["maxLength"] = marker.max_length
    if getattr(marker, "multiple_of", None) is not None:
        param_schema["multipleOf"] = marker.multiple_of
    if marker.regex is not None:
        param_schema["pattern"] = marker.regex
    # OpenAPI 3.1 / JSON Schema 2020-12 — `examples` is an array of
    # sample values on the schema object.
    if getattr(marker, "examples", None):
        param_schema["examples"] = list(marker.examples or [])


def _extract_parameters(
    info: Any, schemas_registry: dict[str, dict]
) -> tuple[list[dict], dict | None, list[tuple[str, dict, bool, bool]]]:
    """Walk the handler signature and classify every parameter.

    Returns `(parameters, request_body_schema, form_fields)`:
    - `parameters` — OpenAPI parameter objects for path/query/header/cookie.
    - `request_body_schema` — schema of the first Pydantic body model (or None).
    - `form_fields` — `(alias, schema, required, is_file)` tuples for
      `Form()` / `File()` params, consumed by `_extract_request_body`.

    Depends/Security and `Body()` markers are intentionally dropped here —
    they belong to other parts of the operation object.
    """
    handler = info.handler
    sig, hints = _handler_intro(handler)
    parameters: list[dict] = []
    request_body_schema: dict | None = None
    form_fields: list[tuple[str, dict, bool, bool]] = []

    if sig is None:
        return parameters, request_body_schema, form_fields

    for pname, param in sig.parameters.items():
        if pname in ("self", "request"):
            continue
        if isinstance(param.default, Depends):
            continue

        annotation = hints.get(pname)

        marker = None
        if isinstance(param.default, ParamBase):
            marker = param.default
            # `include_in_schema=False` — resolved at runtime but omitted.
            if not getattr(marker, "include_in_schema", True):
                continue

        # First BaseModel-typed param becomes the JSON request body.
        if annotation and isinstance(annotation, type) and issubclass(annotation, BaseModel):
            request_body_schema = _pydantic_to_schema(annotation, schemas_registry)
            continue

        # Determine parameter location.
        if marker and isinstance(marker, HeaderParam):
            param_location = "header"
            param_alias = marker.alias or pname
        elif marker and isinstance(marker, CookieParam):
            param_location = "cookie"
            param_alias = marker.alias or pname
        elif marker and isinstance(marker, (FormParam, FileParam)):
            is_file = isinstance(marker, FileParam)
            if is_file:
                field_schema: dict[str, Any] = {"type": "string", "format": "binary"}
            else:
                field_schema = _python_type_to_schema(annotation, schemas_registry)
            if marker.description:
                field_schema["description"] = marker.description
            if getattr(marker, "title", None):
                field_schema["title"] = marker.title
            field_required = not marker.has_default
            field_alias = marker.alias or pname
            form_fields.append((field_alias, field_schema, field_required, is_file))
            continue
        elif marker and isinstance(marker, BodyParam):
            # Body goes into requestBody, not parameters.
            continue
        elif pname in info.param_names or (marker and isinstance(marker, PathParam)):
            param_location = "path"
            param_alias = pname
        else:
            param_location = "query"
            param_alias = marker.alias if marker and marker.alias else pname

        param_schema = _python_type_to_schema(annotation, schemas_registry)

        if marker:
            _apply_marker_constraints(param_schema, marker)

        if marker:
            required = not marker.has_default
            if marker.has_default and marker.default is not ...:
                default_val = marker.default
                if isinstance(default_val, (str, int, float, bool, type(None))):
                    param_schema["default"] = default_val
        elif param_location == "path":
            required = True
        else:
            required = param.default is inspect.Parameter.empty
            if not required and param.default is not inspect.Parameter.empty:
                default_val = param.default
                if isinstance(default_val, (str, int, float, bool, type(None))):
                    param_schema["default"] = default_val

        param_info: dict[str, Any] = {
            "name": param_alias,
            "in": param_location,
            "required": required,
            "schema": param_schema,
        }

        # OpenAPI 3.1 §4.8.12.1 — array-valued query parameters default
        # to `style: form`, `explode: true` (one `?k=v1&k=v2` per item).
        if param_location == "query" and param_schema.get("type") == "array":
            param_info["style"] = "form"
            param_info["explode"] = True

        if marker and marker.deprecated:
            param_info["deprecated"] = True

        parameters.append(param_info)

    return parameters, request_body_schema, form_fields


def _extract_request_body(
    request_body_schema: dict | None,
    form_fields: list[tuple[str, dict, bool, bool]],
) -> dict | None:
    """Build the OpenAPI `requestBody` object, or `None` when no body params exist.

    A JSON Pydantic body takes precedence over form fields, matching the
    monolithic implementation. When only form fields are present, the
    media type is `multipart/form-data` if any field is a file upload
    (OpenAPI 3.1 §4.8.10.4), otherwise `application/x-www-form-urlencoded`.
    """
    if request_body_schema:
        return {
            "required": True,
            "content": {"application/json": {"schema": request_body_schema}},
        }
    if not form_fields:
        return None
    has_file = any(is_file for _, _, _, is_file in form_fields)
    media_type = "multipart/form-data" if has_file else "application/x-www-form-urlencoded"
    properties: dict[str, Any] = {}
    required_fields: list[str] = []
    for fname, fschema, freq, _ in form_fields:
        properties[fname] = fschema
        if freq:
            required_fields.append(fname)
    body_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required_fields:
        body_schema["required"] = required_fields
    return {
        "required": True,
        "content": {media_type: {"schema": body_schema}},
    }


def _extract_responses(info: Any, schemas_registry: dict[str, dict]) -> dict[str, dict]:
    """Build the operation `responses` map.

    Seeds with the success response (re-keyed to `info.status_code` when
    not 200), attaches `response_model` under the primary status, then
    merges entries from `info.responses` — each carrying `model`,
    `description`, or any free-form OpenAPI keys.
    """
    responses: dict[str, dict] = {
        "200": {"description": info.response_description},
    }
    primary_status = str(info.status_code if info.status_code else 200)
    if primary_status != "200":
        # Re-key the seeded default to the route's chosen status.
        responses[primary_status] = responses.pop("200")

    if info.response_model is not None:
        resp_schema = _response_model_to_schema(info.response_model, schemas_registry)
        if resp_schema is not None:
            responses[primary_status]["content"] = {"application/json": {"schema": resp_schema}}

    for status_code, spec in (info.responses or {}).items():
        key = str(status_code)
        existing = responses.setdefault(key, {})
        if not isinstance(spec, dict):
            continue
        extra_model = spec.get("model")
        if extra_model is not None:
            extra_schema = _response_model_to_schema(extra_model, schemas_registry)
            if extra_schema is not None:
                existing.setdefault("content", {})["application/json"] = {"schema": extra_schema}
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


def _walk_webhooks(app: Any, schemas_registry: dict[str, dict]) -> dict[str, Any]:
    """Return the OpenAPI 3.1 `webhooks` map from `app.webhooks`.

    Empty when no webhooks router exists or it has no routes. Each entry
    is keyed by event name (the path on `@app.webhooks.post`) and carries
    one operation per HTTP method registered.
    """
    webhook_items: dict[str, Any] = {}
    webhooks_router = getattr(app, "webhooks", None)
    walker = getattr(webhooks_router, "_walk_routes", None) if webhooks_router else None
    if walker is None:
        return webhook_items
    for wpath, wmethods, winfo in walker():
        event = wpath.strip("/") or wpath
        for wmethod in wmethods:
            op: dict[str, Any] = {
                "summary": winfo.summary or winfo.name,
                "operationId": f"{winfo.name}_{wmethod.lower()}",
                "responses": {"200": {"description": winfo.response_description}},
            }
            if winfo.description:
                op["description"] = winfo.description
            body = _webhook_request_body(winfo.handler, schemas_registry)
            if body is not None:
                op["requestBody"] = {
                    "required": True,
                    "content": {"application/json": {"schema": body}},
                }
            webhook_items.setdefault(event, {})[wmethod.lower()] = op
    return webhook_items


def _build_operation(
    info: Any,
    method_lower: str,
    schemas_registry: dict[str, dict],
    security_schemes_registry: dict[str, dict],
) -> dict[str, Any]:
    """Assemble one OpenAPI operation object for a single route entry."""
    # OpenAPI 3.1 §4.8.10 — operationId must be unique across the document.
    # Explicit override wins; default = `<name>_<method>`.
    op_id = (
        info.operation_id if getattr(info, "operation_id", None) else f"{info.name}_{method_lower}"
    )
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
    # OpenAPI 3.1 §4.8.8 — route-level `callbacks` map emitted verbatim.
    if getattr(info, "callbacks", None):
        operation["callbacks"] = info.callbacks

    parameters, request_body_schema, form_fields = _extract_parameters(info, schemas_registry)
    if parameters:
        operation["parameters"] = parameters

    request_body = _extract_request_body(request_body_schema, form_fields)
    if request_body is not None:
        operation["requestBody"] = request_body

    # Walk this route's Security() chain to register OpenAPI security
    # schemes and attach the operation-level `security` requirement.
    security_requirements = _collect_security_requirements(info, security_schemes_registry)
    if security_requirements:
        operation["security"] = security_requirements

    operation["responses"] = _extract_responses(info, schemas_registry)

    # `openapi_extra` — deep-merge the user-supplied dict over the
    # generated operation. Nested dicts merge key-by-key; scalars and
    # lists in `openapi_extra` overwrite.
    extra = getattr(info, "openapi_extra", None)
    if extra:
        _deep_merge(operation, extra)

    return operation


def get_openapi_schema(app: Any) -> dict:
    """Generate OpenAPI 3.1 schema from the app's registered routes."""
    schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": _build_info_object(app),
        "paths": {},
        "components": {"schemas": {}},
    }

    if getattr(app, "servers", None):
        schema["servers"] = app.servers
    if getattr(app, "openapi_tags", None):
        schema["tags"] = app.openapi_tags
    # OpenAPI 3.1 §4.8.11 — top-level `externalDocs` object.
    if getattr(app, "openapi_external_docs", None):
        schema["externalDocs"] = app.openapi_external_docs

    schemas_registry: dict[str, dict] = {}
    security_schemes_registry: dict[str, dict] = {}

    for method, path, info in app._collect_all_routes():
        method_lower = method.lower()
        if path not in schema["paths"]:
            schema["paths"][path] = {}
        schema["paths"][path][method_lower] = _build_operation(
            info, method_lower, schemas_registry, security_schemes_registry
        )

    webhook_items = _walk_webhooks(app, schemas_registry)
    if webhook_items:
        schema["webhooks"] = webhook_items

    if schemas_registry:
        schema["components"]["schemas"] = schemas_registry
    if security_schemes_registry:
        schema["components"]["securitySchemes"] = security_schemes_registry

    return schema


def _webhook_request_body(handler: Any, registry: dict[str, dict]) -> dict | None:
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
        if annotation and isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return _pydantic_to_schema(annotation, registry)
    return None


def _literal_enum_schema(values: list) -> dict[str, Any]:
    """Build an OpenAPI schema for a fixed set of literal / enum values."""
    schema: dict[str, Any] = {"enum": values}
    if not values:
        return schema
    if all(isinstance(v, bool) for v in values):
        schema["type"] = "boolean"
    elif all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        schema["type"] = "integer"
    elif all(isinstance(v, str) for v in values):
        schema["type"] = "string"
    return schema


def _python_type_to_schema(annotation: Any, registry: dict[str, dict] | None = None) -> dict:
    """Convert a Python type to its OpenAPI 3.1 / JSON Schema 2020-12 form.

    Beyond the primitive scalars this resolves the richer annotations a
    query / path / header / cookie parameter can carry — `datetime`,
    `date`, `time`, `UUID`, `Decimal`, `Enum` subclasses and
    `Literal[...]` — so the emitted parameter schema is complete rather
    than collapsing every non-primitive to a bare string.

    When `registry` is supplied, nested Pydantic models resolve to a
    `$ref` (registering their `$defs`) even inside `list`/`set`/`dict`,
    and multi-member unions emit `anyOf`. Without a registry a model
    degrades to a bare `{"type": "object"}` so direct callers that pass
    only an annotation still get an object-shaped schema.
    """
    if annotation is None or annotation is inspect.Parameter.empty:
        return {"type": "string"}
    # `Any` means "any value allowed" — JSON Schema convention is an empty
    # schema, not a string default. Lets `dict[str, Any]` emit
    # `additionalProperties: {}` rather than forcing string-valued entries.
    if annotation is Any:
        return {}

    origin = get_origin(annotation)
    # Unwrap `Optional[T]` / `T | None` to the inner type so a nullable
    # rich-typed parameter still emits its `format` / `enum` keywords.
    # Genuine multi-member unions emit `anyOf`; a sole `None` member adds
    # `{"type": "null"}` per JSON Schema 2020-12 nullability.
    if origin is Union or origin is _types.UnionType:
        members = get_args(annotation)
        inner = [a for a in members if a is not type(None)]
        has_none = len(inner) != len(members)
        if len(inner) == 1:
            return _python_type_to_schema(inner[0], registry)
        any_of = [_python_type_to_schema(m, registry) for m in inner]
        if has_none:
            any_of.append({"type": "null"})
        return {"anyOf": any_of}

    # Parametrised `list[T]` / `set[T]` → an array schema with typed items.
    if origin in (list, set, tuple):
        args = get_args(annotation)
        item = _python_type_to_schema(args[0], registry) if args else {}
        return {"type": "array", "items": item}
    # Parametrised `dict[K, V]` → an object schema with typed additionalProperties.
    # JSON object keys are strings, so the key type arg is intentionally ignored.
    if origin is dict:
        args = get_args(annotation)
        if len(args) == 2:
            return {
                "type": "object",
                "additionalProperties": _python_type_to_schema(args[1], registry),
            }
        return {"type": "object"}
    # `Literal["a", "b"]` → an enum schema of the literal values.
    if origin is Literal:
        return _literal_enum_schema(list(get_args(annotation)))

    type_map: dict[Any, dict[str, Any]] = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        bytes: {"type": "string", "format": "binary"},
        list: {"type": "array", "items": {}},
        dict: {"type": "object"},
        _datetime.datetime: {"type": "string", "format": "date-time"},
        _datetime.date: {"type": "string", "format": "date"},
        _datetime.time: {"type": "string", "format": "time"},
        _datetime.timedelta: {"type": "string", "format": "duration"},
        _uuid.UUID: {"type": "string", "format": "uuid"},
        Decimal: {"type": "number"},
    }
    mapped = type_map.get(annotation)
    if mapped is not None:
        return mapped

    # `Enum` subclass → an enum schema carrying the member values.
    if isinstance(annotation, type) and issubclass(annotation, _enum.Enum):
        return _literal_enum_schema([member.value for member in annotation])

    # Pydantic model → a `$ref` when a registry is available to hold its
    # `$defs`; otherwise an object-shaped fallback so a registry-less
    # caller still gets `{"type": "object"}` rather than a bare string.
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if registry is not None:
            return _pydantic_to_schema(annotation, registry)
        return {"type": "object"}

    return {"type": "string"}


def _scheme_definition(scheme: Any) -> tuple[str, dict] | None:
    """Inspect a Security() target and return (name, OpenAPI security
    scheme object) for known scheme classes, or None.

    The name is derived from the scheme class so duplicate registrations
    of the same scheme reuse the same components.securitySchemes entry.
    """
    # Deferred imports to avoid circular dependency with security subpackage.
    from veloce.security.api_key import APIKeyCookie, APIKeyHeader, APIKeyQuery
    from veloce.security.http import HTTPBasic, HTTPBearer, HTTPDigest
    from veloce.security.oauth2 import (
        OAuth2AuthorizationCodeBearer,
        OAuth2PasswordBearer,
        OpenIdConnect,
    )

    cls_name = type(scheme).__name__
    if isinstance(scheme, OAuth2PasswordBearer):
        return cls_name, {
            "type": "oauth2",
            "flows": {
                "password": {
                    "tokenUrl": getattr(scheme, "token_url", ""),
                    "scopes": getattr(scheme, "scopes", {}) or {},
                }
            },
        }
    if isinstance(scheme, OAuth2AuthorizationCodeBearer):
        flow: dict[str, Any] = {
            "authorizationUrl": getattr(scheme, "authorizationUrl", ""),
            "tokenUrl": getattr(scheme, "tokenUrl", ""),
            "scopes": getattr(scheme, "scopes", {}) or {},
        }
        refresh = getattr(scheme, "refreshUrl", None)
        if refresh:
            flow["refreshUrl"] = refresh
        return cls_name, {
            "type": "oauth2",
            "flows": {"authorizationCode": flow},
        }
    if isinstance(scheme, OpenIdConnect):
        return cls_name, {
            "type": "openIdConnect",
            "openIdConnectUrl": getattr(scheme, "openIdConnectUrl", ""),
        }
    if isinstance(scheme, HTTPBearer):
        return cls_name, {
            "type": "http",
            "scheme": "bearer",
        }
    if isinstance(scheme, HTTPBasic):
        return cls_name, {
            "type": "http",
            "scheme": "basic",
        }
    if isinstance(scheme, HTTPDigest):
        return cls_name, {
            "type": "http",
            "scheme": "digest",
        }
    if isinstance(scheme, APIKeyHeader):
        return cls_name, {
            "type": "apiKey",
            "in": "header",
            "name": getattr(scheme, "name", ""),
        }
    if isinstance(scheme, APIKeyQuery):
        return cls_name, {
            "type": "apiKey",
            "in": "query",
            "name": getattr(scheme, "name", ""),
        }
    if isinstance(scheme, APIKeyCookie):
        return cls_name, {
            "type": "apiKey",
            "in": "cookie",
            "name": getattr(scheme, "name", ""),
        }
    return None


def _collect_security_requirements(
    info: Any, registry: dict[str, dict]
) -> list[dict[str, list[str]]]:
    """Walk the route's dependency chain, collecting one OpenAPI security
    requirement per `Security(scheme, scopes=[...])` discovered.

    Mutates `registry` to add components.securitySchemes entries.
    Returns the operation-level `security` list — a sequence of
    `{schemeName: [scopes]}` dicts. Empty when no Security() is reachable.
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
                # Don't recurse past a known scheme — its internals are
                # implementation, not policy.
                return
            # Generic dep — recurse into its handler signature.
            inner = target
        else:
            inner = dep

        if inner is None or id(inner) in seen:
            return
        seen.add(id(inner))

        sig, _ = _handler_intro(inner)
        if sig is None:
            return
        for param in sig.parameters.values():
            default = param.default
            if isinstance(default, Depends):
                visit(default)

    # Route-level dependencies (the `dependencies=[Depends(...)]` kwarg).
    for d in getattr(info, "dependencies", ()) or ():
        visit(d)
    # Plus anything in the handler's own parameter defaults.
    handler = info.handler
    sig, _ = _handler_intro(handler)
    if sig is None:
        return requirements
    for param in sig.parameters.values():
        default = param.default
        if isinstance(default, Depends):
            visit(default)
    return requirements


def _response_model_to_schema(response_model: Any, registry: dict[str, dict]) -> dict | None:
    """Render `response_model` into an OpenAPI schema object.

    Handles three shapes:
    - `MyModel` (Pydantic BaseModel subclass) → `{"$ref": ".../MyModel"}`.
    - `list[MyModel]` (or any `Sequence[MyModel]`) → array-of-refs.
    - Anything else → `None` (caller omits the schema).
    """
    origin = get_origin(response_model)
    if origin is list:
        args = get_args(response_model)
        if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            inner = _pydantic_to_schema(args[0], registry)
            return {"type": "array", "items": inner}
        return {"type": "array", "items": {}}

    if isinstance(response_model, type) and issubclass(response_model, BaseModel):
        return _pydantic_to_schema(response_model, registry)

    return None


def _pydantic_to_schema(model: type[BaseModel], registry: dict[str, dict]) -> dict:
    """Convert a Pydantic model to OpenAPI schema, adding to registry."""
    name = model.__name__
    if name not in registry:
        try:
            schema = model.model_json_schema()
            # Extract definitions
            if "$defs" in schema:
                for def_name, def_schema in schema["$defs"].items():
                    registry[def_name] = def_schema
                del schema["$defs"]
            registry[name] = schema
        except Exception as exc:
            # Silently degrading to `{type: object}` hides genuine schema
            # bugs in the user's models. Log loudly at WARNING so the
            # failure surfaces in dev logs, then fall back so /docs still
            # renders (an underspecified schema beats a 500 on /openapi.json).
            _logger.warning(
                "OpenAPI schema generation failed for %s: %s. "
                "Falling back to {type: object}. "
                "Inspect the model definition or attach a debugger to "
                "veloce.openapi to see the full traceback.",
                name,
                exc,
                exc_info=_logger.isEnabledFor(logging.DEBUG),
            )
            registry[name] = {"type": "object"}
    return {"$ref": f"#/components/schemas/{name}"}


# Swagger UI / ReDoc bundles are pinned to a specific patch version and
# loaded with a Subresource Integrity hash. Together with
# `crossorigin="anonymous"` the browser refuses to execute the script
# if the CDN ever serves bytes that do not hash to this exact digest,
# so a CDN compromise cannot inject arbitrary JavaScript onto a
# `/docs` page. Bump the versions in lock-step with the hashes — the
# hash will not match if you change one without the other.
_SWAGGER_UI_VERSION = "5.18.2"
_SWAGGER_UI_CSS_INTEGRITY = "sha512-xRGj65XGEcpPTE7Cn6ujJWokpXVLxqLxdtNZ/n1w52+76XaCRO7UWKZl9yJHvzpk99A0EP6EW+opPcRwPDxwkA=="
_SWAGGER_UI_JS_INTEGRITY = "sha512-9tBcCofqWq+PelL6USpUB7OJrCaObfefi9ht9nVZuKt1XP7eHDs7NwVljLSLVtSsErax1Tz3pG3O82eeq546Rg=="
_REDOC_VERSION = "2.1.5"
_REDOC_JS_INTEGRITY = "sha384-0GrsyTQc9Oqd8h+b2dbc4XdR2T/DYpy0tLNNstyx+LBMUyiBbcWPbEs9aRmUcaxD"

SWAGGER_HTML = (
    """<!DOCTYPE html>
<html>
<head>
    <title>{title} — Swagger UI</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link
      rel="stylesheet"
      href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/__SUV__/swagger-ui.min.css"
      integrity="__SUC__"
      crossorigin="anonymous"
      referrerpolicy="no-referrer">
</head>
<body>
    <div id="swagger-ui"></div>
    <script
      src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/__SUV__/swagger-ui-bundle.min.js"
      integrity="__SUJ__"
      crossorigin="anonymous"
      referrerpolicy="no-referrer"></script>
    <script>
    const ui = SwaggerUIBundle({{
        url: "{openapi_url}",
        dom_id: '#swagger-ui',
        presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
        layout: "StandaloneLayout",
        {ui_params}
    }});
    {init_oauth}
    </script>
</body>
</html>""".replace("__SUV__", _SWAGGER_UI_VERSION)
    .replace("__SUC__", _SWAGGER_UI_CSS_INTEGRITY)
    .replace("__SUJ__", _SWAGGER_UI_JS_INTEGRITY)
)


REDOC_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>{title} — ReDoc</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">
    <style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
    <redoc spec-url='{openapi_url}'></redoc>
    <script
      src="https://unpkg.com/redoc@__RDV__/bundles/redoc.standalone.js"
      integrity="__RDJ__"
      crossorigin="anonymous"
      referrerpolicy="no-referrer"></script>
</body>
</html>""".replace("__RDV__", _REDOC_VERSION).replace("__RDJ__", _REDOC_JS_INTEGRITY)


def setup_openapi_routes(
    app: Any,
    openapi_url: str = "/openapi.json",
    docs_url: str | None = "/docs",
    redoc_url: str | None = "/redoc",
) -> None:
    """Register OpenAPI schema and documentation routes.

    `docs_url` / `redoc_url` of `None` disable the Swagger UI / ReDoc UI
    respectively — the JSON schema route is still registered, so tooling
    can consume the schema without a public interactive explorer.
    """

    @app.get(openapi_url, tags=["openapi"], name="openapi_schema")
    async def openapi_schema(request: Any):
        # Route through `app.openapi()` so a user override / customised
        # `app.openapi_schema` flows to the JSON endpoint and Swagger UI.
        return JSONResponse(app.openapi())

    async def swagger_ui(request: Any):
        # Render extra SwaggerUIBundle options inline as JSON literals.
        # `orjson.dumps` returns bytes, so decode for string concatenation
        # into the HTML template; the surrounding page is utf-8, so
        # orjson's raw-UTF-8 output (vs json's ensure_ascii) is fine.
        params = getattr(app, "swagger_ui_parameters", None) or {}
        if params:
            # Compact `key:value` join — orjson serialises nested values
            # without spaces, so the outer separator stays spaceless to
            # keep the rendered literal consistent throughout.
            ui_params = ",".join(
                f"{orjson.dumps(k).decode()}:{orjson.dumps(v).decode()}" for k, v in params.items()
            )
        else:
            ui_params = ""

        oauth_init = getattr(app, "swagger_ui_init_oauth", None)
        init_oauth = f"ui.initOAuth({orjson.dumps(oauth_init).decode()});" if oauth_init else ""

        html = SWAGGER_HTML.format(
            title=_html.escape(app.title),
            openapi_url=_html.escape(openapi_url),
            ui_params=ui_params,
            init_oauth=init_oauth,
        )
        return HTMLResponse(html)

    async def redoc_ui(request: Any):
        html = REDOC_HTML.format(
            title=_html.escape(app.title), openapi_url=_html.escape(openapi_url)
        )
        return HTMLResponse(html)

    # Register each interactive UI only when its URL is set — a `None`
    # disables that UI while leaving the JSON schema route in place.
    if docs_url is not None:
        app.get(docs_url, tags=["openapi"], name="swagger_ui")(swagger_ui)
    if redoc_url is not None:
        app.get(redoc_url, tags=["openapi"], name="redoc_ui")(redoc_ui)
