"""TestClient.request() — generic verb-agnostic dispatcher."""

from __future__ import annotations

from pydantic import BaseModel

from veloce import JSONResponse, Request, Veloce
from veloce.testclient import TestClient


def _app() -> Veloce:
    app = Veloce()

    @app.get("/g")
    async def g():
        return {"verb": "GET"}

    @app.post("/p")
    async def p(request: Request):
        return {"verb": "POST", "body": await request.json()}

    @app.patch("/pa")
    async def pa():
        return {"verb": "PATCH"}

    @app.delete("/d")
    async def d():
        return {"verb": "DELETE"}

    return app


def test_request_get():
    with TestClient(_app()) as client:
        resp = client.request("GET", "/g")
        assert resp.status_code == 200
        assert resp.json() == {"verb": "GET"}


def test_request_post_with_json():
    with TestClient(_app()) as client:
        resp = client.request("POST", "/p", json={"k": "v"})
        assert resp.json() == {"verb": "POST", "body": {"k": "v"}}


def test_request_patch():
    with TestClient(_app()) as client:
        assert client.request("PATCH", "/pa").json() == {"verb": "PATCH"}


def test_request_delete():
    with TestClient(_app()) as client:
        assert client.request("DELETE", "/d").json() == {"verb": "DELETE"}


def test_request_lowercase_method():
    with TestClient(_app()) as client:
        # Method is upper-cased internally.
        assert client.request("get", "/g").status_code == 200


def test_request_with_params():
    app = Veloce()

    @app.get("/q")
    async def q(request: Request):
        return {"got": request.query_params.get("x")}

    with TestClient(app) as client:
        resp = client.request("GET", "/q", params={"x": "42"})
        assert resp.json() == {"got": "42"}


class _PostJSONItem(BaseModel):
    name: str
    price: float


def test_get():
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index(request: Request):
        return {"hello": "world"}

    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["hello"] == "world"


def test_post_json():
    app = Veloce(openapi_url=None)

    @app.post("/items")
    async def create(item: _PostJSONItem):
        return {"id": 1, "name": item.name}

    client = TestClient(app)
    resp = client.post("/items", json={"name": "Widget", "price": 9.99})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Widget"


def test_post_form_data():
    app = Veloce(openapi_url=None)

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        return {"user": form.get("username")}

    client = TestClient(app)
    resp = client.post("/login", data={"username": "admin", "password": "secret"})
    assert resp.status_code == 200
    assert resp.json()["user"] == "admin"


def test_put():
    app = Veloce(openapi_url=None)

    @app.put("/items/{id}")
    async def update(request: Request, id: int):
        return {"id": id, "updated": True}

    client = TestClient(app)
    resp = client.put("/items/42", json={"name": "Updated"})
    assert resp.status_code == 200
    assert resp.json()["id"] == 42


def test_delete():
    app = Veloce(openapi_url=None)

    @app.delete("/items/{id}")
    async def delete(id: int):
        return {"deleted": id}

    client = TestClient(app)
    resp = client.delete("/items/5")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 5


def test_query_params():
    app = Veloce(openapi_url=None)

    @app.get("/search")
    async def search(q: str = "", page: int = 1):
        return {"q": q, "page": page}

    client = TestClient(app)
    resp = client.get("/search", params={"q": "test", "page": "3"})
    assert resp.json()["q"] == "test"


def test_query_params_in_url():
    app = Veloce(openapi_url=None)

    @app.get("/search")
    async def search(q: str = ""):
        return {"q": q}

    client = TestClient(app)
    resp = client.get("/search?q=hello")
    assert resp.json()["q"] == "hello"


def test_custom_headers():
    app = Veloce(openapi_url=None)

    @app.get("/echo-header")
    async def echo(request: Request):
        # Try both cases since headers may come in as-is from TestClient
        return {"ua": request.headers.get("user-agent", request.headers.get("User-Agent", ""))}

    client = TestClient(app)
    resp = client.get("/echo-header", headers={"user-agent": "TestBot"})
    assert resp.json()["ua"] == "TestBot"


def test_cookie_tracking():
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


def test_text_response():
    app = Veloce(openapi_url=None)

    @app.get("/text")
    async def text(request: Request):
        return "hello"

    client = TestClient(app)
    resp = client.get("/text")
    assert resp.text == "hello"


class TestTestClientNoWarning:
    """Verify TestClient doesn't trigger pytest collection warning."""

    def test_testclient_has_test_false(self):

        assert TestClient.__test__ is False
