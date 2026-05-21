"""app.openapi() / app.openapi_schema / app.openapi_version ."""

from __future__ import annotations

from veloce import Veloce


def test_openapi_method_returns_dict():
    app = Veloce()

    @app.get("/x")
    async def x():
        return {}

    schema = app.openapi()
    assert isinstance(schema, dict)
    assert "openapi" in schema
    assert "paths" in schema


def test_openapi_method_is_cached():
    app = Veloce()

    @app.get("/x")
    async def x():
        return {}

    first = app.openapi()
    second = app.openapi()
    assert first is second


def test_openapi_schema_overrides_generation():
    """Assigning to openapi_schema bypasses the auto-build."""
    app = Veloce()

    custom = {"openapi": "3.1.0", "info": {"title": "custom"}, "paths": {}}
    app.openapi_schema = custom
    assert app.openapi() is custom


def test_openapi_schema_starts_none():
    app = Veloce()
    assert app.openapi_schema is None


def test_openapi_method_caches_into_attribute():
    """After calling openapi(), openapi_schema is populated."""
    app = Veloce()
    assert app.openapi_schema is None
    schema = app.openapi()
    assert app.openapi_schema is schema


def test_openapi_version_default():
    app = Veloce()
    assert app.openapi_version == "3.1.0"


def test_openapi_version_attribute_writable():
    app = Veloce()
    app.openapi_version = "3.0.2"
    assert app.openapi_version == "3.0.2"


def test_openapi_method_overridable_in_subclass():
    """Pattern: users override app.openapi() to customise the schema."""

    class CustomApp(Veloce):
        def openapi(self):
            return {"custom": True}

    app = CustomApp()
    assert app.openapi() == {"custom": True}


def test_openapi_json_endpoint_serves_overridden_schema():
    """Setting `app.openapi_schema` makes /openapi.json return that exact dict."""
    import orjson

    from veloce.testclient import TestClient

    app = Veloce()

    @app.get("/x")
    async def x():
        return {}

    app.openapi_schema = {"openapi": "3.1.0", "info": {"title": "override"}, "paths": {}}
    with TestClient(app) as client:
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        assert orjson.loads(resp.body)["info"]["title"] == "override"


def test_openapi_schema_mutation_persists():
    """User can mutate the cached dict in place — UI sees the change."""
    app = Veloce()

    @app.get("/x")
    async def x():
        return {}

    schema = app.openapi()
    schema["info"]["x-logo"] = {"url": "https://example.com/logo.png"}
    # Second call returns same dict with mutation.
    again = app.openapi()
    assert again["info"]["x-logo"] == {"url": "https://example.com/logo.png"}


def test_docs_url_none_disables_swagger_ui_only():
    client = Veloce(docs_url=None).test_client()
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 200


def test_redoc_url_none_disables_redoc_only():
    client = Veloce(redoc_url=None).test_client()
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/redoc").status_code == 404
    assert client.get("/docs").status_code == 200
