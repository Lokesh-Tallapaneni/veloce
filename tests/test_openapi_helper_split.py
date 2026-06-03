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
    _build_info_object,
    _extract_parameters,
    _extract_request_body,
    _extract_responses,
    _route_has_validatable_input,
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


# Golden byte sequence for the fixture above under orjson with sorted
# keys (routes with validatable input advertise the injected 422). Drift
# in either length or sha256 means an unintended change in output.
_EXPECTED_BYTES_LEN = 3287
_EXPECTED_SHA256 = "12f1eb9665304fdc709a960d75ec7c006800109142d0b501dc5e1fbd062501a7"


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
    schemas_registry: dict = {}
    params, body_schema, form_fields = _extract_parameters(info, schemas_registry)
    request_body = _extract_request_body(body_schema, form_fields)
    has_validatable_input = _route_has_validatable_input(params, body_schema, form_fields)
    responses = _extract_responses(info, schemas_registry, has_validatable_input)

    op = full["paths"]["/items"][method.lower()]
    assert op["requestBody"] == request_body
    assert op["responses"] == responses
    assert params == op.get("parameters", [])

    webhooks = _walk_webhooks(app, schemas_registry)
    assert webhooks == full["webhooks"]
