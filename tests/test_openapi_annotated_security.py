"""An `Annotated[..., Security(...)]` route publishes its security requirement.

`_handler_intro` called `get_type_hints(handler)` without `include_extras=True`,
so PEP 593 metadata was stripped before the OpenAPI dependency walkers saw it.
Both walkers then classified a parameter solely by `isinstance(param.default,
Depends)` — which the `Annotated` form never sets, because the marker lives in
the annotation.

    /a  (cred = Security(bearer))                    security: [{'HTTPBearer': []}]
    /b  (cred: Annotated[object, Security(bearer)])  security: None

The route is still *enforced* — measured, `/b` answers 401 without a credential
— so this was never an authentication hole. It is the published contract that
was wrong, and wrong in the direction that matters: a generated client believes
the endpoint is open, and the scheme never reaches `components.securitySchemes`.

It also hit the recommended form. `Annotated` is the house style in
`.claude/rules/style-guide.md` for user-facing signatures, so the documented way
to write a secured route was the one that published itself as unsecured.

The marker resolution is not reimplemented here. `_handler_plan` already does it
correctly — that is why runtime enforcement worked — including peeling the
implicit `Optional` wrapper Python 3.10 adds around `Annotated` for a parameter
defaulting to `None`. That logic is now a shared helper both callers use, so the
two doors cannot answer differently again.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from pydantic import BaseModel

from veloce import (
    APIKeyHeader,
    Depends,
    HTTPBearer,
    Query,
    Security,
    Veloce,
)
from veloce.testclient import TestClient

bearer = HTTPBearer()
api_key = APIKeyHeader(name="X-API-Key")


class Item(BaseModel):
    id: int


async def current_user(cred: Annotated[object, Security(bearer)]) -> str:
    """Module scope - see `test_an_annotated_dependency_wrapping_a_scheme_is_published`."""
    return "ada"


def _schema(build) -> dict:
    app = Veloce(title="T", version="1")
    build(app)
    return app.openapi()


def _security_for(schema: dict, path: str, method: str = "get"):
    return schema["paths"][path][method].get("security")


# ── the two spellings agree ──────────────────────────────────────────


def test_an_annotated_security_dependency_is_published():
    """The defect: this published `security: None`."""

    def build(app):
        @app.get("/b")
        async def b(cred: Annotated[object, Security(bearer)]) -> dict:
            return {}

    assert _security_for(_schema(build), "/b") == [{"HTTPBearer": []}]


def test_both_spellings_publish_the_same_requirement():
    def build(app):
        @app.get("/a")
        async def a(cred=Security(bearer)) -> dict:
            return {}

        @app.get("/b")
        async def b(cred: Annotated[object, Security(bearer)]) -> dict:
            return {}

    schema = _schema(build)
    assert _security_for(schema, "/a") == _security_for(schema, "/b")


def test_the_scheme_reaches_components():
    """A requirement naming a scheme absent from components is unresolvable."""

    def build(app):
        @app.get("/b")
        async def b(cred: Annotated[object, Security(bearer)]) -> dict:
            return {}

    schema = _schema(build)
    assert "HTTPBearer" in schema["components"]["securitySchemes"]


def test_annotated_security_with_scopes_publishes_them():
    def build(app):
        @app.get("/b")
        async def b(cred: Annotated[object, Security(bearer, scopes=["read", "write"])]) -> dict:
            return {}

    assert _security_for(_schema(build), "/b") == [{"HTTPBearer": ["read", "write"]}]


def test_an_annotated_dependency_wrapping_a_scheme_is_published():
    """The common shape: a `current_user` dependency that itself takes the scheme.

    `current_user` is at module scope on purpose. This file uses PEP 563, so a
    dependency defined inside the test would be a closure variable that
    `get_type_hints` cannot resolve when it evaluates the annotation string -
    and *both* doors fail on that shape identically (the runtime answers 422,
    treating the parameter as a required query value), so it is a Python
    limitation rather than the divergence this file is about.
    """

    def build(app):
        @app.get("/me")
        async def me(user: Annotated[str, Depends(current_user)]) -> dict:
            return {}

    assert _security_for(_schema(build), "/me") == [{"HTTPBearer": []}]


def test_two_annotated_schemes_are_both_published():
    def build(app):
        @app.get("/two")
        async def two(
            a: Annotated[object, Security(bearer)],
            b: Annotated[object, Security(api_key)],
        ) -> dict:
            return {}

    requirements = _security_for(_schema(build), "/two")
    names = {name for requirement in requirements for name in requirement}
    assert names == {"HTTPBearer", "APIKeyHeader"}


# ── the runtime door was always right, and stays right ───────────────


def test_the_annotated_route_is_still_enforced():
    """The negative direction: publishing it must not have changed enforcement."""
    app = Veloce(title="T", version="1", openapi_url=None)

    @app.get("/b")
    async def b(cred: Annotated[object, Security(bearer)]) -> dict:
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/b").status_code == 401
    assert client.get("/b", headers={"Authorization": "Bearer x"}).status_code == 200


def test_an_unsecured_route_publishes_no_requirement():
    """The other negative: not everything becomes secured."""

    def build(app):
        @app.get("/open")
        async def open_route() -> dict:
            return {}

    assert _security_for(_schema(build), "/open") is None


# ── `include_extras` must not disturb the other hint consumers ───────


def test_an_annotated_query_parameter_still_documents_its_type():
    """The hints feed parameter typing too - `Annotated` must be unwrapped there."""

    def build(app):
        @app.get("/q")
        async def q(limit: Annotated[int, Query(ge=1)] = 10) -> dict:
            return {}

    params = _schema(build)["paths"]["/q"]["get"]["parameters"]
    limit = next(p for p in params if p["name"] == "limit")
    assert limit["schema"]["type"] == "integer"


def test_an_annotated_model_body_is_still_recognised():
    def build(app):
        @app.post("/i")
        async def create(item: Annotated[Item, Depends(lambda: Item(id=1))]) -> dict:
            return {}

    # The route builds and documents without error; the dependency is not a body.
    assert "/i" in _schema(build)["paths"]


def test_a_plain_query_parameter_is_unaffected():
    def build(app):
        @app.get("/p")
        async def p(limit: int = 10) -> dict:
            return {}

    params = _schema(build)["paths"]["/p"]["get"]["parameters"]
    assert next(p for p in params if p["name"] == "limit")["schema"]["type"] == "integer"


def test_a_model_response_is_still_documented():
    def build(app):
        @app.get("/m")
        async def m() -> Item:
            return Item(id=1)

    schema = _schema(build)
    assert "Item" in schema["components"]["schemas"]


# ── the 3.10 implicit-Optional shape the plan builder already handles ─


def test_an_optional_annotated_security_parameter_is_published():
    """On 3.10 `get_type_hints` wraps a `None`-defaulted parameter in `Optional`,
    putting `Annotated` inside it. The shared helper peels that."""

    def build(app):
        @app.get("/opt")
        async def opt(cred: Annotated[object, Security(bearer)] = None) -> dict:
            return {}

    assert _security_for(_schema(build), "/opt") == [{"HTTPBearer": []}]


def test_an_explicitly_optional_annotated_parameter_is_published():
    def build(app):
        @app.get("/opt2")
        async def opt2(cred: Annotated[object, Security(bearer)] | None = None) -> dict:
            return {}

    assert _security_for(_schema(build), "/opt2") == [{"HTTPBearer": []}]


# ── the shared helper itself ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("annotation", "expected_marker"),
    [
        (Annotated[object, Security(bearer)], True),
        (Annotated[int, Query(ge=1)], True),
        (int, False),
        (Annotated[int, "just a string"], False),
    ],
    ids=["security", "query", "bare", "unrelated-metadata"],
)
def test_the_helper_extracts_only_veloce_markers(annotation, expected_marker):
    from veloce._handler_plan import extract_annotated_marker

    marker, _base = extract_annotated_marker(annotation)
    assert (marker is not None) is expected_marker


def test_the_helper_returns_the_inner_type():
    from veloce._handler_plan import extract_annotated_marker

    _marker, base = extract_annotated_marker(Annotated[int, Query(ge=1)])
    assert base is int


def test_the_helper_leaves_a_bare_annotation_alone():
    from veloce._handler_plan import extract_annotated_marker

    marker, base = extract_annotated_marker(int)
    assert marker is None
    assert base is int
