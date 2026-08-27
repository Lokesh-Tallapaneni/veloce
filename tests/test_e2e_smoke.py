"""End-to-end smoke test - the full stack through `TestClient`.

One realistic API - two middlewares, three request hooks, teardown, bearer
auth, fourteen routes and a sub-router - and nine tests walking different
paths through it.

The builder used to be a 130-line `_make_app` method on a test class, called
explicitly at the top of all nine tests. It is an `app` fixture now: the same
per-test isolation, without each test repeating the call, and without a class
that existed only to hold it.

One handler wrote `request._state["session"] = session` to save a session it
had read out of `request.state`. `SessionMiddleware` is installed here, so
`request.session` *is* that dict and mutating it is the whole operation - the
reach-in was doing by hand what the accessor does.
"""

import datetime

import pytest
from pydantic import BaseModel

from veloce import (
    CORSMiddleware,
    Depends,
    HTMLResponse,
    HTTPBearer,
    Query,
    Request,
    Response,
    Router,
    SessionMiddleware,
    TestClient,
    Veloce,
    abort,
    g,
    jsonable_encoder,
    jsonify,
    make_response,
    status,
)


class Item(BaseModel):
    name: str
    price: float
    description: str | None = None


@pytest.fixture
def app() -> Veloce:
    app = Veloce(
        title="E2E Test API",
        version="1.0.0",
        description="Smoke test",
        openapi_url="/openapi.json",
        redirect_slashes=True,
    )

    app.config["DB_URL"] = "sqlite:///:memory:"
    app.secret_key = "test-secret"
    app.state["items"] = {}

    # Middleware
    app.add_middleware(CORSMiddleware(allow_origins=["*"]))
    app.add_middleware(SessionMiddleware(secret_key="test-session-key"))

    # HTTP middleware
    @app.middleware("http")
    async def add_server_header(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Server"] = "Veloce"
        response._encoded = None
        return response

    # Before/after hooks
    @app.before_request
    async def log_request(request: Request):
        g.request_path = request.path

    @app.after_request
    async def add_version(request: Request, response: Response):
        response.headers["X-API-Version"] = "1.0.0"
        response._encoded = None
        return response

    # Teardown
    @app.teardown_request
    async def cleanup(exc):
        pass  # Just verify it runs without error

    # Auth
    bearer = HTTPBearer(auto_error=False)

    # Routes
    @app.get("/")
    async def index(request: Request):
        return {"message": "Welcome", "config_db": request.app.config["DB_URL"]}

    @app.get("/items", response_model=list[Item], status_code=status.HTTP_200_OK)
    async def list_items(
        request: Request,
        q: str = Query(default=""),
        limit: int = Query(default=10, ge=1, le=100),
    ):
        items = list(request.app.state["items"].values())
        if q:
            items = [i for i in items if q.lower() in i["name"].lower()]
        return items[:limit]

    @app.post("/items", status_code=status.HTTP_201_CREATED)
    async def create_item(item: Item, request: Request):
        item_id = len(request.app.state["items"]) + 1
        request.app.state["items"][item_id] = item.model_dump()
        return {"id": item_id, **item.model_dump()}

    @app.get("/items/{item_id}")
    async def get_item(item_id: int, request: Request):
        if item_id not in request.app.state["items"]:
            abort(404, "Item not found")
        return {"id": item_id, **request.app.state["items"][item_id]}

    @app.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_item(item_id: int, request: Request):
        if item_id not in request.app.state["items"]:
            abort(404)
        del request.app.state["items"][item_id]
        return Response(status_code=204, body=b"")

    @app.get("/protected")
    async def protected(token=Depends(bearer)):
        if token is None:
            abort(401, "Token required")
        return {"token": token}

    @app.get("/session")
    async def session_test(request: Request):
        # `SessionMiddleware` is installed above, so `request.session` *is*
        # the session - a dict mutated in place. Reading it out of
        # `request.state` and writing it back through the private
        # `request._state` was doing by hand what the accessor already does.
        request.session["count"] = request.session.get("count", 0) + 1
        return {"count": request.session["count"]}

    @app.get("/g-test")
    async def g_test(request: Request):
        return {"path": g.request_path}

    @app.get("/html", response_class=HTMLResponse)
    async def html_page(request: Request):
        return "<h1>Hello HTML</h1>"

    @app.get("/tuple-response")
    async def tuple_resp(request: Request):
        return {"created": True}, 201, {"X-Custom": "header"}

    @app.get("/jsonify")
    async def jsonify_test(request: Request):
        return jsonify(framework="veloce", fast=True)

    @app.get("/make-response")
    async def make_resp(request: Request):
        return make_response("custom body", 200)

    @app.get("/encoder")
    async def encoder_test(request: Request):
        data = {"date": datetime.date(2024, 1, 15), "items": {1, 2, 3}}
        return jsonable_encoder(data)

    # Sub-router
    api_v2 = Router(prefix="/api/v2", tags=["v2"])

    @api_v2.get("/ping")
    async def ping(request: Request):
        return {"pong": True}

    app.include_router(api_v2)

    return app


def test_full_crud_flow(app):
    client = TestClient(app)

    # GET /
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "Welcome"
    assert "X-Server" in resp.headers
    assert "X-API-Version" in resp.headers

    # POST /items
    resp = client.post("/items", json={"name": "Widget", "price": 9.99})
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] == 1
    assert data["name"] == "Widget"

    # GET /items
    resp = client.get("/items")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1

    # GET /items?q=widget
    resp = client.get("/items", params={"q": "widget"})
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # GET /items/1
    resp = client.get("/items/1")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Widget"

    # GET /items/999
    resp = client.get("/items/999")
    assert resp.status_code == 404

    # DELETE /items/1
    resp = client.delete("/items/1")
    assert resp.status_code == 204


def test_auth_flow(app):
    client = TestClient(app)

    # No token
    resp = client.get("/protected")
    assert resp.status_code == 401

    # With token
    resp = client.get("/protected", headers={"authorization": "Bearer mytoken"})
    assert resp.status_code == 200
    assert resp.json()["token"] == "mytoken"


def test_middleware_and_hooks(app):
    client = TestClient(app)

    resp = client.get("/g-test")
    assert resp.status_code == 200
    assert resp.json()["path"] == "/g-test"


def test_response_types(app):
    client = TestClient(app)

    # HTML response
    resp = client.get("/html")
    assert b"<h1>Hello HTML</h1>" in resp.body

    # Tuple response
    resp = client.get("/tuple-response")
    assert resp.status_code == 201
    assert resp.headers.get("X-Custom") == "header"

    # jsonify
    resp = client.get("/jsonify")
    data = resp.json()
    assert data["framework"] == "veloce"

    # make_response
    resp = client.get("/make-response")
    assert resp.text == "custom body"

    # jsonable_encoder
    resp = client.get("/encoder")
    data = resp.json()
    assert "2024-01-15" in data["date"]


def test_subrouter(app):
    client = TestClient(app)

    resp = client.get("/api/v2/ping")
    assert resp.status_code == 200
    assert resp.json()["pong"] is True


def test_openapi_schema(app):
    client = TestClient(app)

    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert schema["info"]["title"] == "E2E Test API"
    assert "/items" in schema["paths"]
    assert "/items/{item_id}" in schema["paths"]


def test_swagger_ui(app):
    client = TestClient(app)

    resp = client.get("/docs")
    assert resp.status_code == 200
    assert b"swagger-ui" in resp.body


def test_cors_preflight(app):
    client = TestClient(app)

    resp = client.options("/items", headers={"origin": "http://example.com"})
    assert resp.status_code == 204
    assert "Access-Control-Allow-Origin" in resp.headers


def test_redirect_slashes(app):

    @app.get("/users/")
    async def users(request: Request):
        return [{"id": 1}]

    client = TestClient(app)
    resp = client.get("/users")
    assert resp.status_code == 307
