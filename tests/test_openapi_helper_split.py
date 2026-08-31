"""Regression test guarding the split of `get_openapi_schema` into helpers.

The schema produced by the orchestrator must match a known-good document
captured from a representative fixture app (path/query/header parameters with
constraints, JSON body, form fields with files, OAuth-less HTTP bearer
security, custom status code, extra response models, deprecated route, and a
webhook). Any drift means the refactor leaked a behaviour change.

The baseline is the pretty-printed document in
`tests/data/openapi_helper_split_baseline.json`, compared structurally. It used
to be a byte length and a sha256, which caught drift but reported it as two
opaque numbers: an author who changed the schema on purpose was told the length
went from 3414 to 3452 and left to find out what moved. Now the failure names
the differing paths, and rebaselining is a readable diff rather than two
numbers nobody can review.
"""

from __future__ import annotations

import pathlib
from typing import Annotated

import orjson
import pytest
from pydantic import BaseModel

from veloce import File, Form, Header, Query, Veloce
from veloce.contrib.openapi import (
    SchemaRegistry,
    _build_info_object,
    _dependency_graph_has_validatable,
    _extract_parameters,
    _extract_request_body,
    _extract_responses,
    _has_validatable_params,
    _repoint_validation_error_refs,
    _walk_webhooks,
    get_openapi_schema,
)
from veloce.dependency import Depends, Security
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


# Captured from the monolithic implementation prior to the helper split, and
# rebaselined once since: `HTTPValidationError` gained a `status_code`
# property, because the 422 the dispatcher emits carries one and the schema
# that omitted it described a body no client receives.
_BASELINE = pathlib.Path(__file__).parent / "data" / "openapi_helper_split_baseline.json"


def _differences(expected: object, actual: object, path: str = "$") -> list[str]:
    """Every leaf on which the two documents disagree, named by JSON path.

    A sha256 answers whether they differ. This answers where, which is what
    the author of an intentional schema change needs and what a reviewer needs
    to judge the rebaselined file.
    """
    if type(expected) is not type(actual):
        return [f"{path}: {type(expected).__name__} -> {type(actual).__name__}"]
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        out = []
        for key in sorted(set(expected) | set(actual)):
            if key not in actual:
                out.append(f"{path}.{key}: removed")
            elif key not in expected:
                out.append(f"{path}.{key}: added ({actual[key]!r})")
            else:
                out += _differences(expected[key], actual[key], f"{path}.{key}")
        return out
    if isinstance(expected, list):
        assert isinstance(actual, list)
        if len(expected) != len(actual):
            return [f"{path}: {len(expected)} entries -> {len(actual)}"]
        out = []
        for index, (want, got) in enumerate(zip(expected, actual)):
            out += _differences(want, got, f"{path}[{index}]")
        return out
    return [] if expected == actual else [f"{path}: {expected!r} -> {actual!r}"]


def test_get_openapi_schema_orchestrator_matches_the_baseline() -> None:
    expected = orjson.loads(_BASELINE.read_bytes())
    actual = get_openapi_schema(_fixture_app())
    drift = _differences(expected, actual)
    if drift:
        listing = "\n  ".join(drift)
        raise AssertionError(
            f"the generated document no longer matches {_BASELINE.name}:"
            f"\n  {listing}\n\nIf the change is intentional, regenerate the "
            "baseline and review the diff."
        )


def test_the_document_is_deterministic() -> None:
    """Caches must not perturb output between two calls on one app."""
    app = _fixture_app()
    first = orjson.dumps(get_openapi_schema(app), option=orjson.OPT_SORT_KEYS)
    second = orjson.dumps(get_openapi_schema(app), option=orjson.OPT_SORT_KEYS)
    assert first == second


def test_the_baseline_is_the_document_it_claims_to_be() -> None:
    """The stored bytes are what `orjson` writes for the parsed document.

    A baseline edited by hand into something the generator cannot produce would
    fail every run afterwards with a diff nobody could act on.
    """
    # Line endings are normalised first: `orjson` emits LF, and a checkout with
    # `core.autocrlf` on hands this CRLF. That is not schema drift.
    raw = _BASELINE.read_bytes().replace(b"\r\n", b"\n")
    reserialised = orjson.dumps(
        orjson.loads(raw), option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2
    )
    assert raw.rstrip(b"\n") == reserialised


@pytest.mark.parametrize(
    ("mutate", "expected_path"),
    [
        (lambda d: d["paths"].pop("/items"), "$.paths./items: removed"),
        (lambda d: d["info"].__setitem__("title", "other"), "$.info.title:"),
        (lambda d: d.__setitem__("openapi", ["3.1.0"]), "$.openapi: str -> list"),
        (lambda d: d["paths"].__setitem__("/new", {}), "$.paths./new: added"),
    ],
)
def test_a_changed_document_is_reported_by_path(mutate, expected_path) -> None:
    """The differ is the guard; a differ that returns nothing guards nothing."""
    expected = orjson.loads(_BASELINE.read_bytes())
    actual = orjson.loads(_BASELINE.read_bytes())
    mutate(actual)
    drift = _differences(expected, actual)
    assert any(entry.startswith(expected_path) for entry in drift), drift


def test_an_identical_document_reports_no_difference() -> None:
    expected = orjson.loads(_BASELINE.read_bytes())
    assert _differences(expected, orjson.loads(_BASELINE.read_bytes())) == []


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
    inputs = _extract_parameters(info, schemas_registry)
    request_body = _extract_request_body(
        inputs.request_body_schema, inputs.form_fields, inputs.body_fields, inputs.scalar_body
    )
    # The orchestrator's own predicate, not a copy of it. This was written out
    # here and had already drifted - it omitted the `_dependency_graph_has_validatable`
    # disjunct, so it agreed only for routes whose 422 comes from their own
    # parameters or body, which is the case this fixture happens to be. A route
    # validated solely through a dependency would have been compared against the
    # wrong expectation, which is the thing this parity test exists to rule out.
    has_validatable_params = _has_validatable_params(inputs, request_body, info)
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
    assert inputs.parameters == op.get("parameters", [])
    assert document["webhooks"] == full["webhooks"]


# ── the disjunct the parity copy had dropped ─────────────────────────
#
# `has_validatable_params` has three terms, and the parity check above hand-wrote
# it with the third - "some dependency in the graph consumes validated input" -
# missing. For most shapes that is harmless, because a dependency's `Query` /
# `Header` / `Cookie` parameters are hoisted into the route's own `parameters`
# and a dependency's body model becomes the route's `requestBody`, so one of the
# first two terms is already true.
#
# It is load-bearing for exactly one shape: a parameter that is **validated but
# suppressed from the schema**. `Query(include_in_schema=False)` inside a
# dependency still raises `RequestValidationError` on bad input, but contributes
# no `parameters` entry and no `requestBody` - so the third term is the only
# thing that puts a `422` in the document. That is the case the dropped term
# covers and the case nothing compared.


def _hidden_param_app() -> Veloce:
    """A route whose only validatable input is hidden from the schema."""
    app = Veloce(title="Hidden", version="1")

    async def hidden(x: Annotated[int, Query(include_in_schema=False)] = 1):
        return x

    @app.get("/hidden")
    async def hidden_route(v=Depends(hidden)):
        return {}

    @app.get("/bare")
    async def bare_route():
        return {}

    return app


def test_only_the_third_disjunct_fires_for_a_hidden_parameter():
    """The premise of the tests below: the other two terms really are false."""
    app = _hidden_param_app()
    _m, _p, info = next((m, p, i) for m, p, i in app._collect_all_routes() if p == "/hidden")
    registry = SchemaRegistry()
    inputs = _extract_parameters(info, registry)
    request_body = _extract_request_body(
        inputs.request_body_schema, inputs.form_fields, inputs.body_fields, inputs.scalar_body
    )
    assert inputs.parameters == []
    assert request_body is None
    assert _dependency_graph_has_validatable(info) is True


def test_a_hidden_validatable_parameter_still_advertises_a_422():
    schema = _hidden_param_app().openapi()
    assert "422" in schema["paths"]["/hidden"]["get"]["responses"]


def test_a_route_with_nothing_validatable_does_not():
    """The negative: without it the assertion above would be vacuous."""
    schema = _hidden_param_app().openapi()
    assert "422" not in schema["paths"]["/bare"]["get"]["responses"]


def test_the_dropped_disjunct_changes_the_answer():
    """Stated at the predicate: the two-term expression the parity copy carried
    disagrees with the orchestrator's three-term one on this route."""
    app = _hidden_param_app()
    _m, _p, info = next((m, p, i) for m, p, i in app._collect_all_routes() if p == "/hidden")
    registry = SchemaRegistry()
    inputs = _extract_parameters(info, registry)
    request_body = _extract_request_body(
        inputs.request_body_schema, inputs.form_fields, inputs.body_fields, inputs.scalar_body
    )
    dropped = bool(inputs.parameters) or request_body is not None
    correct = dropped or _dependency_graph_has_validatable(info)
    assert dropped is False
    assert correct is True


def test_the_helpers_and_the_orchestrator_agree_on_that_route():
    """The parity check itself, run against the case the dropped term covers."""
    app = _hidden_param_app()
    full = get_openapi_schema(app)
    _m, _p, info = next((m, p, i) for m, p, i in app._collect_all_routes() if p == "/hidden")
    registry = SchemaRegistry()
    inputs = _extract_parameters(info, registry)
    request_body = _extract_request_body(
        inputs.request_body_schema, inputs.form_fields, inputs.body_fields, inputs.scalar_body
    )
    flag = (
        bool(inputs.parameters)
        or request_body is not None
        or _dependency_graph_has_validatable(info)
    )
    responses = _extract_responses(info, registry, flag)
    document = {"responses": responses}
    registry.finalize(document)
    _repoint_validation_error_refs(document, "HTTPValidationError")
    assert set(document["responses"]) == set(full["paths"]["/hidden"]["get"]["responses"])
