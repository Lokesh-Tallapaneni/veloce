"""Regression test guarding the split of `get_openapi_schema` into helpers.

The schema produced by the orchestrator must match a known-good byte
sequence captured from a representative fixture app (path/query/header
params with constraints, JSON body, form fields with files, OAuth-less
HTTP bearer security, custom status code, extra response models,
deprecated route, and a webhook). Any byte-level drift means the
refactor leaked a behavior change.
"""

from __future__ import annotations

import hashlib

import orjson
from pydantic import BaseModel

from veloce import Veloce
from veloce.contrib.openapi import (
    SchemaRegistry,
    _build_info_object,
    _extract_parameters,
    _extract_request_body,
    _extract_responses,
    _repoint_validation_error_refs,
    _walk_webhooks,
    get_openapi_schema,
)
from veloce.dependency import Security
from veloce.routing.params import File, Form, Header, Query
from veloce.security.http import HTTPBearer


class _Item(BaseModel):
    name: str
    price: float


class _Out(BaseModel):
    id: int


def _fixture_app() -> Veloce:
    bearer = HTTPBearer()
    app = Veloce()

    @app.get(
        "/items/{item_id}",
        response_model=_Out,
        tags=["x"],
        summary="Get item",
        deprecated=True,
    )
    async def get_item(
        request,
        item_id: int,
        q: str = Query(default="hi", max_length=10, examples=["a", "b"]),
        h: str = Header(default="v"),
        tok=Security(bearer),
    ):
        return {"id": item_id}

    @app.post(
        "/items",
        responses={
            400: {"model": _Out, "description": "bad"},
            500: {"description": "server"},
        },
        status_code=201,
    )
    async def create_item(request, body: _Item):
        return {}

    @app.post("/upload")
    async def upload(request, name: str = Form(), file: bytes = File()):
        return {}

    @app.post("/login")
    async def login(request, username: str = Form(), password: str = Form()):
        return {}

    @app.webhooks.post("order.created")
    async def order_hook(request, body: _Item):
        return {}

    return app


# Captured from the monolithic implementation prior to the helper split.
# orjson serialization with sorted keys yields exactly this byte sequence
# for the fixture above. Drift in either length or sha256 means the
# refactor altered observable output.
_EXPECTED_BYTES_LEN = 3359
_EXPECTED_SHA256 = "976330372234fe156a0993c8515adb7165821e125fc3e917745f58ee0075ff9e"


def test_get_openapi_schema_orchestrator_byte_identical() -> None:
    app = _fixture_app()
    schema = get_openapi_schema(app)
    payload = orjson.dumps(schema, option=orjson.OPT_SORT_KEYS)
    assert len(payload) == _EXPECTED_BYTES_LEN
    assert hashlib.sha256(payload).hexdigest() == _EXPECTED_SHA256

    # Re-running must be deterministic — caches must not perturb output.
    second = orjson.dumps(get_openapi_schema(app), option=orjson.OPT_SORT_KEYS)
    assert payload == second


def test_get_openapi_schema_helpers_assemble_same_operation() -> None:
    """Running the helpers directly and reassembling matches the orchestrator."""
    app = _fixture_app()
    full = get_openapi_schema(app)

    # Spot-check info object is identical to the helper output.
    assert _build_info_object(app) == full["info"]

    # Spot-check a single route — the POST /items entry must equal what
    # _extract_parameters + _extract_request_body + _extract_responses build.
    routes = list(app._collect_all_routes())
    method, path, info = next((m, p, i) for m, p, i in routes if p == "/items" and m == "POST")
    schemas_registry = SchemaRegistry()
    params, body_schema, form_fields, body_fields, scalar_body = _extract_parameters(
        info, schemas_registry
    )
    request_body = _extract_request_body(body_schema, form_fields, body_fields, scalar_body)
    # POST /items carries a JSON body, so its request is validatable and the
    # 422 response is auto-added — mirror the orchestrator's argument.
    has_validatable_params = bool(params) or request_body is not None
    responses = _extract_responses(info, schemas_registry, has_validatable_params)
    # `_walk_webhooks` appends each webhook's auto operationId to `auto_ops` for
    # the document-wide disambiguation pass; the list is unused here.
    webhook_auto_ops: list = []
    webhooks = _walk_webhooks(app, schemas_registry, webhook_auto_ops)
    # The helpers emit placeholder refs; finalize rewrites them into the same
    # `#/components/schemas/...` form the orchestrator produces.
    document = {
        "paths": {"/items": {method.lower(): {"responses": responses}}},
        "requestBody": request_body,
        "responses": responses,
        "webhooks": webhooks,
    }
    schemas_registry.finalize(document)
    # Mirror the orchestrator's document-level step: resolve the auto-422
    # placeholder ref to the finalized envelope name (no collision -> canonical).
    _repoint_validation_error_refs(document, "HTTPValidationError")

    op = full["paths"]["/items"][method.lower()]
    assert document["requestBody"] == op["requestBody"]
    assert document["responses"] == op["responses"]
    assert params == op.get("parameters", [])
    assert document["webhooks"] == full["webhooks"]
