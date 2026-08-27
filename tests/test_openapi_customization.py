"""app.openapi() / app.openapi_schema / app.openapi_version.

The module held two test eras. The first is bare functions on `TestClient` and
`app.openapi()`; the second was four classes that called the **private**
`app._setup_openapi()`, dispatched through `handle_request(make_request(...))`
and hand-decoded the body with `orjson.loads(resp.body)` - each marked
`@pytest.mark.asyncio`, although `asyncio_mode = "auto"` has made that redundant
for the whole suite.

None of that was about OpenAPI. `TestClient` runs the startup `_setup_openapi`
was standing in for, `resp.json()` is the decode, and a test that awaits nothing
does not need to be `async`. The second era is converted to the first, so both
halves read the same way and neither reaches into the app.
"""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Request, Veloce
from veloce.testclient import TestClient


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

    app = Veloce()

    @app.get("/x")
    async def x():
        return {}

    app.openapi_schema = {"openapi": "3.1.0", "info": {"title": "override"}, "paths": {}}
    with TestClient(app) as client:
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        assert resp.json()["info"]["title"] == "override"


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


def test_openapi_with_metadata():
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
    schema = TestClient(app).get("/openapi.json").json()

    assert schema["info"]["title"] == "My API"
    assert schema["info"]["description"] == "A test API"
    assert schema["info"]["contact"]["name"] == "Dev"
    assert schema["info"]["license"]["name"] == "MIT"
    assert schema["info"]["termsOfService"] == "https://example.com/tos"
    assert schema["servers"][0]["url"] == "https://api.example.com"
    assert schema["tags"][0]["name"] == "users"


def test_openapi_schema_generation():
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


def test_openapi_route():
    app = Veloce(title="Test API")

    @app.get("/hello")
    async def hello(request: Request):
        return {"hello": "world"}

    resp = TestClient(app).get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "paths" in schema


def test_swagger_ui():
    app = Veloce()
    resp = TestClient(app).get("/docs")
    assert resp.status_code == 200
    assert b"swagger-ui" in resp.body


def test_redoc_ui():
    app = Veloce()
    resp = TestClient(app).get("/redoc")
    assert resp.status_code == 200
    assert b"redoc" in resp.body


def test_docs_pages_carry_the_csp_nonce():
    # Under a nonced policy the docs pages must nonce every script, style,
    # and stylesheet link, using the same nonce the header advertises -
    # otherwise the inline SwaggerUIBundle boot script cannot execute.
    import re

    from veloce.middleware.security import CSPMiddleware

    app = Veloce()
    app.add_middleware(CSPMiddleware, policy="default-src 'self'; script-src {nonce}")
    client = app.test_client()

    for path in ("/docs", "/redoc"):
        resp = client.get(path)
        assert resp.status_code == 200
        nonces = re.findall(r'nonce="([^"]+)"', resp.text)
        assert nonces, f"{path} carries no nonce attribute"
        assert len(set(nonces)) == 1, f"{path} used more than one nonce"
        header = next(v for k, v in resp.headers.items() if k.lower() == "content-security-policy")
        assert f"'nonce-{nonces[0]}'" in header


def test_docs_pages_omit_nonce_without_csp():
    # No CSP middleware means no nonce is armed; the markup must stay
    # byte-identical to the pre-nonce output rather than emit `nonce=""`.
    app = Veloce()
    page = app.test_client().get("/docs").text
    assert 'nonce="' not in page
    assert "SwaggerUIBundle" in page


def test_openapi_disabled():
    app = Veloce(openapi_url=None)
    resp = TestClient(app).get("/openapi.json")
    assert resp.status_code == 404


def test_deprecated_route():
    app = Veloce(openapi_url=None)

    @app.get("/old", deprecated=True, summary="Old endpoint")
    async def old(request: Request):
        return {"old": True}

    from veloce.contrib.openapi import get_openapi_schema

    schema = get_openapi_schema(app)
    assert schema["paths"]["/old"]["get"]["deprecated"] is True
