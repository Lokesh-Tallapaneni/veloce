"""app.openapi() / app.openapi_schema / app.openapi_version ."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from tests.conftest import make_request
from veloce import Request, Veloce


class _OpenAPIItem(BaseModel):
    name: str
    price: float


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


class TestOpenAPIMetadata:
    @pytest.mark.asyncio
    async def test_openapi_with_metadata(self):
        app = Veloce(
            title="My API",
            version="2.0.0",
            description="A test API",
            contact={"name": "Dev", "email": "dev@example.com"},
            license_info={"name": "MIT"},
            terms_of_service="https://example.com/tos",
            servers=[{"url": "https://api.example.com", "description": "Production"}],
            openapi_tags=[{"name": "users", "description": "User operations"}],
        )
        app._setup_openapi()

        resp = await app.handle_request(make_request(path="/openapi.json"))
        import orjson

        schema = orjson.loads(resp.body)

        assert schema["info"]["title"] == "My API"
        assert schema["info"]["description"] == "A test API"
        assert schema["info"]["contact"]["name"] == "Dev"
        assert schema["info"]["license"]["name"] == "MIT"
        assert schema["info"]["termsOfService"] == "https://example.com/tos"
        assert schema["servers"][0]["url"] == "https://api.example.com"
        assert schema["tags"][0]["name"] == "users"


class TestOpenAPI:
    @pytest.mark.asyncio
    async def test_openapi_schema_generation(self):
        app = Veloce(title="Test API", version="1.0.0", openapi_url=None)

        @app.get("/items/{item_id}", tags=["items"], summary="Get an item")
        async def get_item(item_id: int, q: str = ""):
            return {"id": item_id}

        @app.post("/items", tags=["items"])
        async def create_item(item: _OpenAPIItem):
            return item.model_dump()

        from veloce.contrib.openapi import get_openapi_schema

        schema = get_openapi_schema(app)

        assert schema["info"]["title"] == "Test API"
        assert schema["info"]["version"] == "1.0.0"
        assert "/items/{item_id}" in schema["paths"]
        assert "/items" in schema["paths"]
        assert "get" in schema["paths"]["/items/{item_id}"]

        get_op = schema["paths"]["/items/{item_id}"]["get"]
        assert get_op["summary"] == "Get an item"
        assert "items" in get_op["tags"]
        # Should have path param and query param
        params = get_op["parameters"]
        param_names = [p["name"] for p in params]
        assert "item_id" in param_names
        assert "q" in param_names

        # POST should have request body
        post_op = schema["paths"]["/items"]["post"]
        assert "requestBody" in post_op

    @pytest.mark.asyncio
    async def test_openapi_route(self):
        app = Veloce(title="Test API")
        app._setup_openapi()

        @app.get("/hello")
        async def hello(request: Request):
            return {"hello": "world"}

        resp = await app.handle_request(make_request(path="/openapi.json"))
        assert resp.status_code == 200
        import orjson

        schema = orjson.loads(resp.body)
        assert "paths" in schema

    @pytest.mark.asyncio
    async def test_swagger_ui(self):
        app = Veloce()
        app._setup_openapi()

        resp = await app.handle_request(make_request(path="/docs"))
        assert resp.status_code == 200
        assert b"swagger-ui" in resp.body

    @pytest.mark.asyncio
    async def test_redoc_ui(self):
        app = Veloce()
        app._setup_openapi()

        resp = await app.handle_request(make_request(path="/redoc"))
        assert resp.status_code == 200
        assert b"redoc" in resp.body

    @pytest.mark.asyncio
    async def test_openapi_disabled(self):
        app = Veloce(openapi_url=None)
        app._setup_openapi()

        resp = await app.handle_request(make_request(path="/openapi.json"))
        assert resp.status_code == 404


class TestRouteMetadata:
    @pytest.mark.asyncio
    async def test_deprecated_route(self):
        app = Veloce(openapi_url=None)

        @app.get("/old", deprecated=True, summary="Old endpoint")
        async def old(request: Request):
            return {"old": True}

        from veloce.contrib.openapi import get_openapi_schema

        schema = get_openapi_schema(app)
        assert schema["paths"]["/old"]["get"]["deprecated"] is True
