"""OpenAPI top-level externalDocs — Veloce(openapi_external_docs=...)."""

from __future__ import annotations

from veloce import Veloce
from veloce.contrib.openapi import get_openapi_schema


def test_external_docs_emitted_when_set():
    ext = {"description": "Full docs", "url": "https://docs.example.com"}
    app = Veloce(openapi_external_docs=ext)
    schema = get_openapi_schema(app)
    assert schema["externalDocs"] == ext


def test_no_external_docs_means_no_field():
    app = Veloce()
    schema = get_openapi_schema(app)
    assert "externalDocs" not in schema


def test_external_docs_attribute_default_none():
    assert Veloce().openapi_external_docs is None


def test_external_docs_attribute_set():
    app = Veloce(openapi_external_docs={"url": "https://x.test"})
    assert app.openapi_external_docs == {"url": "https://x.test"}
