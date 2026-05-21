"""Tests for all new features — TestClient, UploadFile, cookies, url_for,
security, sessions, OpenAPI, SSE, hooks, dependency overrides, etc."""

import pytest
from pydantic import BaseModel

from veloce import (
    URL,
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    Depends,
    EventSourceResponse,
    FormData,
    Headers,
    HTMLResponse,
    HTTPBasic,
    HTTPBearer,
    JSONResponse,
    OAuth2PasswordBearer,
    Request,
    Response,
    ServerSentEvent,
    SessionMiddleware,
    UploadFile,
    Veloce,
)
from veloce.middleware import (
    RateLimitMiddleware,
    RequestIDMiddleware,
)
from veloce.testclient import TestClient

# ── Helper ──────────────────────────────────────────────────────


def make_request(method="GET", path="/", headers=None, body=b"", query_string=""):
    return Request(
        method=method,
        path=path,
        query_string=query_string,
        headers=headers or {},
        body=body,
    )


# ═══════════════════════════════════════════════════════════════
# TestClient tests
# ═══════════════════════════════════════════════════════════════


class TestTestClient:
    def test_get(self):
        app = Veloce(openapi_url=None)

        @app.get("/")
        async def index(request: Request):
            return {"hello": "world"}

        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json()["hello"] == "world"

    def test_post_json(self):
        app = Veloce(openapi_url=None)

        class Item(BaseModel):
            name: str
            price: float

        @app.post("/items")
        async def create(item: Item):
            return {"id": 1, "name": item.name}

        client = TestClient(app)
        resp = client.post("/items", json={"name": "Widget", "price": 9.99})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Widget"

    def test_post_form_data(self):
        app = Veloce(openapi_url=None)

        @app.post("/login")
        async def login(request: Request):
            form = await request.form()
            return {"user": form.get("username")}

        client = TestClient(app)
        resp = client.post("/login", data={"username": "admin", "password": "secret"})
        assert resp.status_code == 200
        assert resp.json()["user"] == "admin"

    def test_put(self):
        app = Veloce(openapi_url=None)

        @app.put("/items/{id}")
        async def update(request: Request, id: int):
            return {"id": id, "updated": True}

        client = TestClient(app)
        resp = client.put("/items/42", json={"name": "Updated"})
        assert resp.status_code == 200
        assert resp.json()["id"] == 42

    def test_delete(self):
        app = Veloce(openapi_url=None)

        @app.delete("/items/{id}")
        async def delete(id: int):
            return {"deleted": id}

        client = TestClient(app)
        resp = client.delete("/items/5")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 5

    def test_query_params(self):
        app = Veloce(openapi_url=None)

        @app.get("/search")
        async def search(q: str = "", page: int = 1):
            return {"q": q, "page": page}

        client = TestClient(app)
        resp = client.get("/search", params={"q": "test", "page": "3"})
        assert resp.json()["q"] == "test"

    def test_query_params_in_url(self):
        app = Veloce(openapi_url=None)

        @app.get("/search")
        async def search(q: str = ""):
            return {"q": q}

        client = TestClient(app)
        resp = client.get("/search?q=hello")
        assert resp.json()["q"] == "hello"

    def test_custom_headers(self):
        app = Veloce(openapi_url=None)

        @app.get("/echo-header")
        async def echo(request: Request):
            # Try both cases since headers may come in as-is from TestClient
            return {"ua": request.headers.get("user-agent", request.headers.get("User-Agent", ""))}

        client = TestClient(app)
        resp = client.get("/echo-header", headers={"user-agent": "TestBot"})
        assert resp.json()["ua"] == "TestBot"

    def test_cookie_tracking(self):
        app = Veloce(openapi_url=None)

        @app.get("/set-cookie")
        async def set_cookie(request: Request):
            resp = JSONResponse({"ok": True})
            resp.set_cookie("token", "abc123")
            return resp

        @app.get("/read-cookie")
        async def read_cookie(request: Request):
            return {"token": request.cookies.get("token", "")}

        client = TestClient(app)
        client.get("/set-cookie")
        resp = client.get("/read-cookie")
        assert resp.json()["token"] == "abc123"

    def test_text_response(self):
        app = Veloce(openapi_url=None)

        @app.get("/text")
        async def text(request: Request):
            return "hello"

        client = TestClient(app)
        resp = client.get("/text")
        assert resp.text == "hello"


# ═══════════════════════════════════════════════════════════════
# Request enhancements
# ═══════════════════════════════════════════════════════════════


class TestRequestEnhancements:
    def test_cookies_parsing(self):
        req = make_request(headers={"cookie": "session=abc123; theme=dark"})
        assert req.cookies["session"] == "abc123"
        assert req.cookies["theme"] == "dark"

    def test_url_construction(self):
        req = make_request(
            path="/api/v1/users",
            query_string="page=1",
            headers={"host": "example.com:8080"},
        )
        url = req.url
        assert url.host == "example.com"
        assert url.port == 8080
        assert url.path == "/api/v1/users"
        assert str(url) == "http://example.com:8080/api/v1/users?page=1"

    def test_base_url(self):
        req = make_request(headers={"host": "api.example.com"})
        assert req.base_url == "http://api.example.com"

    def test_is_json(self):
        req = make_request(headers={"content-type": "application/json"})
        assert req.is_json is True

    def test_is_form(self):
        req = make_request(headers={"content-type": "application/x-www-form-urlencoded"})
        assert req.is_form is True

    def test_user_agent(self):
        req = make_request(headers={"user-agent": "Mozilla/5.0"})
        assert req.user_agent == "Mozilla/5.0"

    def test_content_length(self):
        req = make_request(headers={"content-length": "42"})
        assert req.content_length == 42

    @pytest.mark.asyncio
    async def test_text(self):
        req = make_request(body=b"hello world")
        text = await req.text()
        assert text == "hello world"

    def test_authorization(self):
        req = make_request(headers={"authorization": "Bearer token123"})
        assert req.authorization == "Bearer token123"


# ═══════════════════════════════════════════════════════════════
# UploadFile & FormData
# ═══════════════════════════════════════════════════════════════


class TestUploadFile:
    @pytest.mark.asyncio
    async def test_upload_file_read(self):
        import io

        f = UploadFile(filename="test.txt", file=io.BytesIO(b"hello"))
        data = await f.read()
        assert data == b"hello"

    @pytest.mark.asyncio
    async def test_upload_file_content(self):
        import io

        f = UploadFile(filename="test.txt", file=io.BytesIO(b"content"))
        assert f.content == b"content"

    def test_upload_file_repr(self):
        f = UploadFile(filename="photo.jpg", content_type="image/jpeg", size=1024)
        assert "photo.jpg" in repr(f)

    @pytest.mark.asyncio
    async def test_multipart_file_upload(self):
        app = Veloce(openapi_url=None)

        @app.post("/upload")
        async def upload(request: Request):
            form = await request.form()
            file = form.get("file")
            if isinstance(file, UploadFile):
                content = await file.read()
                return {"filename": file.filename, "size": len(content)}
            return {"error": "no file"}

        # Build multipart body
        body = (
            b"------TestBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="test.txt"\r\n'
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"Hello World\r\n"
            b"------TestBoundary--\r\n"
        )

        req = make_request(
            method="POST",
            path="/upload",
            body=body,
            headers={"content-type": "multipart/form-data; boundary=----TestBoundary"},
        )
        resp = await app.handle_request(req)
        import orjson

        data = orjson.loads(resp.body)
        assert data["filename"] == "test.txt"
        assert data["size"] == 11


# ═══════════════════════════════════════════════════════════════
# URL and Headers
# ═══════════════════════════════════════════════════════════════


class TestDataStructures:
    def test_url_replace(self):
        url = URL(scheme="http", host="example.com", path="/api")
        new_url = url.replace(scheme="https")
        assert new_url.scheme == "https"
        assert new_url.host == "example.com"

    def test_url_netloc_default_port(self):
        url = URL(host="example.com", port=80)
        assert url.netloc == "example.com"

    def test_url_netloc_custom_port(self):
        url = URL(host="example.com", port=9000)
        assert url.netloc == "example.com:9000"

    def test_headers_case_insensitive(self):
        h = Headers({"Content-Type": "application/json"})
        assert h.get("content-type") == "application/json"
        assert h.get("CONTENT-TYPE") == "application/json"

    def test_formdata_getlist(self):
        # FormData is a MultiDict — repeated keys are stored as separate
        # entries, not a list-as-value. Construction from a list of tuples
        # is the multi-value idiom.
        fd = FormData([("items", "a"), ("items", "b"), ("items", "c")])
        assert fd.getlist("items") == ["a", "b", "c"]
        assert fd.getlist("missing") == []


# ═══════════════════════════════════════════════════════════════
# url_for
# ═══════════════════════════════════════════════════════════════


class TestUrlFor:
    def test_simple_url_for(self):
        app = Veloce(openapi_url=None)

        @app.get("/users", name="list_users")
        async def users(request: Request):
            return []

        assert app.url_for("list_users") == "/users"

    def test_url_for_with_params(self):
        app = Veloce(openapi_url=None)

        @app.get("/users/{user_id}/posts/{post_id}")
        async def get_post(user_id: int, post_id: int):
            return {}

        url = app.url_for("get_post", user_id="42", post_id="7")
        assert url == "/users/42/posts/7"

    def test_url_for_missing_param(self):
        app = Veloce(openapi_url=None)

        @app.get("/users/{id}")
        async def get_user(id: int):
            return {}

        from veloce import BuildError

        with pytest.raises(BuildError):
            app.url_for("get_user")

    def test_url_for_unknown_route(self):
        from veloce import BuildError

        app = Veloce(openapi_url=None)
        with pytest.raises(BuildError):
            app.url_for("nonexistent")


# ═══════════════════════════════════════════════════════════════
# Before/After request hooks
# ═══════════════════════════════════════════════════════════════


class TestRequestHooks:
    @pytest.mark.asyncio
    async def test_before_request(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.before_request
        async def log_request(request: Request):
            log.append(f"{request.method} {request.path}")
            return None  # Continue to handler

        @app.get("/test")
        async def test(request: Request):
            return {"ok": True}

        await app.handle_request(make_request(path="/test"))
        assert log == ["GET /test"]

    @pytest.mark.asyncio
    async def test_before_request_short_circuit(self):
        app = Veloce(openapi_url=None)

        @app.before_request
        async def block(request: Request):
            if request.path == "/blocked":
                return JSONResponse({"error": "blocked"}, status_code=403)
            return None

        @app.get("/blocked")
        async def blocked(request: Request):
            return {"should_not": "reach"}

        resp = await app.handle_request(make_request(path="/blocked"))
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_after_request(self):
        app = Veloce(openapi_url=None)

        @app.after_request
        async def add_header(request: Request, response: Response):
            response.headers["X-Custom"] = "added"
            response._encoded = None
            return response

        @app.get("/data")
        async def data(request: Request):
            return {"data": True}

        resp = await app.handle_request(make_request(path="/data"))
        assert resp.headers.get("X-Custom") == "added"


# ═══════════════════════════════════════════════════════════════
# Status code error handlers
# ═══════════════════════════════════════════════════════════════


class TestStatusCodeHandlers:
    @pytest.mark.asyncio
    async def test_custom_404_handler(self):
        app = Veloce(openapi_url=None)

        @app.exception_handler(404)
        async def custom_404(request: Request):
            return HTMLResponse("<h1>Custom 404</h1>", status_code=404)

        resp = await app.handle_request(make_request(path="/nonexistent"))
        assert resp.status_code == 404
        assert b"Custom 404" in resp.body

    @pytest.mark.asyncio
    async def test_custom_500_handler(self):
        app = Veloce(openapi_url=None)

        @app.exception_handler(500)
        async def custom_500(request: Request):
            return JSONResponse({"error": "custom 500"}, status_code=500)

        @app.get("/crash")
        async def crash(request: Request):
            raise RuntimeError("boom")

        resp = await app.handle_request(make_request(path="/crash"))
        assert resp.status_code == 500
        assert b"custom 500" in resp.body


# ═══════════════════════════════════════════════════════════════
# Security
# ═══════════════════════════════════════════════════════════════


class TestSecurity:
    @pytest.mark.asyncio
    async def test_http_bearer(self):
        app = Veloce(openapi_url=None)
        security = HTTPBearer()

        @app.get("/protected")
        async def protected(token=Depends(security)):
            return {"token": token}

        resp = await app.handle_request(
            make_request(path="/protected", headers={"authorization": "Bearer mytoken123"})
        )
        assert resp.status_code == 200
        import orjson

        assert orjson.loads(resp.body)["token"] == "mytoken123"

    @pytest.mark.asyncio
    async def test_http_bearer_missing(self):
        app = Veloce(openapi_url=None)
        security = HTTPBearer()

        @app.get("/protected")
        async def protected(token=Depends(security)):
            return {"token": token}

        resp = await app.handle_request(make_request(path="/protected"))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_http_basic(self):
        app = Veloce(openapi_url=None)
        security = HTTPBasic()

        @app.get("/admin")
        async def admin(credentials=Depends(security)):
            return {"user": credentials.username}

        import base64

        creds = base64.b64encode(b"admin:secret").decode()
        resp = await app.handle_request(
            make_request(path="/admin", headers={"authorization": f"Basic {creds}"})
        )
        assert resp.status_code == 200
        import orjson

        assert orjson.loads(resp.body)["user"] == "admin"

    @pytest.mark.asyncio
    async def test_api_key_header(self):
        app = Veloce(openapi_url=None)
        api_key = APIKeyHeader(name="X-API-Key")

        @app.get("/data")
        async def data(key=Depends(api_key)):
            return {"key": key}

        resp = await app.handle_request(
            make_request(path="/data", headers={"x-api-key": "secret123"})
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_api_key_query(self):
        app = Veloce(openapi_url=None)
        api_key = APIKeyQuery(name="api_key")

        @app.get("/data")
        async def data(key=Depends(api_key)):
            return {"key": key}

        resp = await app.handle_request(make_request(path="/data", query_string="api_key=mykey"))
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_api_key_cookie(self):
        app = Veloce(openapi_url=None)
        api_key = APIKeyCookie(name="token")

        @app.get("/data")
        async def data(key=Depends(api_key)):
            return {"key": key}

        resp = await app.handle_request(
            make_request(path="/data", headers={"cookie": "token=cookiekey"})
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_oauth2_password_bearer(self):
        app = Veloce(openapi_url=None)
        oauth2 = OAuth2PasswordBearer(token_url="/token")

        @app.get("/me")
        async def me(token=Depends(oauth2)):
            return {"token": token}

        resp = await app.handle_request(
            make_request(path="/me", headers={"authorization": "Bearer jwt.token.here"})
        )
        assert resp.status_code == 200
        import orjson

        assert orjson.loads(resp.body)["token"] == "jwt.token.here"


# ═══════════════════════════════════════════════════════════════
# Sessions
# ═══════════════════════════════════════════════════════════════


class TestSessions:
    @pytest.mark.asyncio
    async def test_session_set_and_read(self):
        app = Veloce(openapi_url=None)
        app.add_middleware(SessionMiddleware(secret_key="test-secret-key"))

        @app.get("/set")
        async def set_session(request: Request):
            request.state["session"]["username"] = "alice"
            return {"ok": True}

        @app.get("/get")
        async def get_session(request: Request):
            return {"username": request.state.get("session", {}).get("username", "")}

        # Set session
        resp = await app.handle_request(make_request(path="/set"))
        assert resp.status_code == 200
        assert "Set-Cookie" in resp.headers

        # Extract cookie
        cookie = resp.headers["Set-Cookie"]
        cookie_val = cookie.split(";")[0].split("=", 1)[1]

        # Read session
        resp2 = await app.handle_request(
            make_request(path="/get", headers={"cookie": f"session={cookie_val}"})
        )
        import orjson

        data = orjson.loads(resp2.body)
        assert data["username"] == "alice"

    def test_session_signing(self):
        from veloce.middleware.sessions import SessionMiddleware

        mw = SessionMiddleware(secret_key="test")

        # Sign and verify via the underlying Signer.
        encoded = mw._signer.dumps({"user": "alice"})
        decoded = mw._signer.loads(encoded)
        assert decoded["user"] == "alice"

        # Tampered cookie should fail (raises rather than returning None now).
        from veloce.signing import BadSignature

        tampered = encoded[:-5] + "xxxxx"
        with pytest.raises(BadSignature):
            mw._signer.loads(tampered)


# ═══════════════════════════════════════════════════════════════
# Dependency overrides
# ═══════════════════════════════════════════════════════════════


class TestDependencyOverrides:
    @pytest.mark.asyncio
    async def test_override(self):
        app = Veloce(openapi_url=None)

        def get_db():
            return {"real": True}

        def get_mock_db():
            return {"mock": True}

        @app.get("/db")
        async def db_route(db=Depends(get_db)):
            return db

        # Without override
        resp = await app.handle_request(make_request(path="/db"))
        import orjson

        assert orjson.loads(resp.body)["real"] is True

        # With override
        app._dependency_overrides[get_db] = get_mock_db
        resp = await app.handle_request(make_request(path="/db"))
        assert orjson.loads(resp.body)["mock"] is True

        # Clean up
        del app._dependency_overrides[get_db]


# ═══════════════════════════════════════════════════════════════
# OpenAPI
# ═══════════════════════════════════════════════════════════════


class TestOpenAPI:
    @pytest.mark.asyncio
    async def test_openapi_schema_generation(self):
        app = Veloce(title="Test API", version="1.0.0", openapi_url=None)

        class Item(BaseModel):
            name: str
            price: float

        @app.get("/items/{item_id}", tags=["items"], summary="Get an item")
        async def get_item(item_id: int, q: str = ""):
            return {"id": item_id}

        @app.post("/items", tags=["items"])
        async def create_item(item: Item):
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


# ═══════════════════════════════════════════════════════════════
# SSE
# ═══════════════════════════════════════════════════════════════


class TestSSE:
    def test_event_encoding(self):
        event = ServerSentEvent(data="hello", event="message", id="1")
        encoded = event.encode()
        assert b"id: 1" in encoded
        assert b"event: message" in encoded
        assert b"data: hello" in encoded

    def test_event_multiline(self):
        event = ServerSentEvent(data="line1\nline2")
        encoded = event.encode()
        assert b"data: line1" in encoded
        assert b"data: line2" in encoded

    def test_event_retry(self):
        event = ServerSentEvent(data="test", retry=5000)
        encoded = event.encode()
        assert b"retry: 5000" in encoded


# ═══════════════════════════════════════════════════════════════
# Additional middleware
# ═══════════════════════════════════════════════════════════════


class TestAdditionalMiddleware:
    @pytest.mark.asyncio
    async def test_request_id_middleware(self):
        app = Veloce(openapi_url=None)
        app.add_middleware(RequestIDMiddleware())

        @app.get("/")
        async def index(request: Request):
            return {"request_id": request.state.get("request_id", "")}

        resp = await app.handle_request(make_request())
        assert "X-Request-ID" in resp.headers

    @pytest.mark.asyncio
    async def test_request_id_preserved(self):
        app = Veloce(openapi_url=None)
        app.add_middleware(RequestIDMiddleware())

        @app.get("/")
        async def index(request: Request):
            return {"id": request.state["request_id"]}

        resp = await app.handle_request(make_request(headers={"x-request-id": "custom-id-123"}))
        assert resp.headers["X-Request-ID"] == "custom-id-123"

    @pytest.mark.asyncio
    async def test_rate_limit(self):
        app = Veloce(openapi_url=None)
        app.add_middleware(RateLimitMiddleware(max_requests=2, window_seconds=60))

        @app.get("/")
        async def index(request: Request):
            return {"ok": True}

        # First two requests should pass
        for _ in range(2):
            resp = await app.handle_request(make_request())
            assert resp.status_code == 200

        # Third should be rate limited
        resp = await app.handle_request(make_request())
        assert resp.status_code == 429


# ═══════════════════════════════════════════════════════════════
# Mount sub-apps
# ═══════════════════════════════════════════════════════════════


class TestMountSubApps:
    @pytest.mark.asyncio
    async def test_mount(self):
        main = Veloce(openapi_url=None)
        sub = Veloce(openapi_url=None)

        @sub.get("/items")
        async def items(request: Request):
            return [{"id": 1}]

        main.mount("/api", sub)

        resp = await main.handle_request(make_request(path="/api/items"))
        assert resp.status_code == 200
        import orjson

        assert orjson.loads(resp.body) == [{"id": 1}]


# ═══════════════════════════════════════════════════════════════
# Route metadata (deprecated, description)
# ═══════════════════════════════════════════════════════════════


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


def test_eventsource_response_accepts_serversentevent_objects():
    """EventSourceResponse encodes yielded ServerSentEvent objects over ASGI."""
    app = Veloce(openapi_url=None)

    @app.get("/sse")
    async def sse(request):
        async def generate():
            yield ServerSentEvent(data="hello", event="greeting")

        return EventSourceResponse(generate())

    resp = app.test_client().get("/sse")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.content_type
    assert b"data: hello" in resp.body
    assert b"event: greeting" in resp.body


async def test_rate_limit_middleware_evicts_stale_buckets():
    """The bucket dict must not grow unbounded with unique client IPs."""
    import time as _time

    mw = RateLimitMiddleware(max_requests=1000, window_seconds=1)
    now = _time.monotonic()
    stale = now - 3600
    mw._buckets = {f"stale-{i}": [stale] for i in range(100)}
    mw._buckets["fresh"] = [now]  # a live bucket — must survive the sweep
    mw._last_sweep = stale  # force the next request to trigger a sweep

    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    await mw.process_request(req)

    # The 100 stale buckets are evicted; the live bucket is kept.
    assert not any(k.startswith("stale-") for k in mw._buckets)
    assert "fresh" in mw._buckets
