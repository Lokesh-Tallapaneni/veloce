"""Query/Path `include_in_schema=False` — hide a parameter from OpenAPI."""

from __future__ import annotations

from tests._openapi import document, parameters
from veloce import Query, Veloce
from veloce.testclient import TestClient


def test_hidden_param_absent_from_schema():
    app = Veloce()

    @app.get("/x")
    async def x(internal: str = Query(default="", include_in_schema=False)):
        return {}

    with TestClient(app) as client:
        schema = document(client)

    names = [p["name"] for p in parameters(schema, "/x")]
    assert "internal" not in names


def test_visible_param_still_present():
    app = Veloce()

    @app.get("/x")
    async def x(
        shown: str = Query(default=""),
        hidden: str = Query(default="", include_in_schema=False),
    ):
        return {}

    with TestClient(app) as client:
        schema = document(client)

    names = [p["name"] for p in parameters(schema, "/x")]
    assert "shown" in names
    assert "hidden" not in names


def test_hidden_param_still_resolved_at_runtime():
    app = Veloce()

    @app.get("/x")
    async def x(internal: str = Query(default="fallback", include_in_schema=False)):
        return {"internal": internal}

    with TestClient(app) as client:
        assert client.get("/x?internal=live").json() == {"internal": "live"}
        assert client.get("/x").json() == {"internal": "fallback"}


def test_default_include_in_schema_is_true():
    q = Query(default="")
    assert q.include_in_schema is True
