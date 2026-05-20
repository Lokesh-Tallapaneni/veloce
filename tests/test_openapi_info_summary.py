"""Veloce(summary=...) emitted as OpenAPI 3.1 info.summary."""

from __future__ import annotations

from veloce import Veloce
from veloce.testclient import TestClient


def test_summary_emitted_into_info():
    app = Veloce(summary="A concise API summary.")

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    assert schema["info"]["summary"] == "A concise API summary."


def test_no_summary_key_when_unset():
    app = Veloce()

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    assert "summary" not in schema["info"]


def test_summary_coexists_with_description():
    app = Veloce(summary="Short.", description="A much longer description.")

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    assert schema["info"]["summary"] == "Short."
    assert schema["info"]["description"] == "A much longer description."


def test_summary_attribute_on_app():
    app = Veloce(summary="X")
    assert app.summary == "X"
