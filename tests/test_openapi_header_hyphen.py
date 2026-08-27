"""Header param underscore->hyphen normalization in the OpenAPI document."""

from __future__ import annotations

from tests._openapi import parameters
from veloce import Header, Veloce
from veloce.testclient import TestClient


def test_unaliased_header_documents_hyphenated():
    app = Veloce()

    @app.get("/a")
    async def a(x_token: str = Header()):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    params = parameters(schema, "/a")
    headers = [p for p in params if p["in"] == "header"]
    names = {p["name"] for p in headers}
    assert "x-token" in names
    assert "x_token" not in names


def test_convert_underscores_false_keeps_raw():
    app = Veloce()

    @app.get("/b")
    async def b(x_token: str = Header(convert_underscores=False)):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    headers = [p for p in parameters(schema, "/b") if p["in"] == "header"]
    assert {p["name"] for p in headers} == {"x_token"}


def test_explicit_alias_wins():
    app = Veloce()

    @app.get("/c")
    async def c(tok: str = Header(alias="X-Custom")):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    headers = [p for p in parameters(schema, "/c") if p["in"] == "header"]
    assert {p["name"] for p in headers} == {"X-Custom"}
