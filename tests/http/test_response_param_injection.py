"""Response parameter injection.

A handler (or dependency) may declare `response: Response`; Veloce
injects a fresh Response whose `status_code` / `headers` / cookies are
merged onto the final response when the handler returns a plain value.
"""

from __future__ import annotations

from veloce import Depends, Response, Veloce
from veloce.testclient import TestClient


def test_handler_sets_status_code():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(response: Response):
        response.status_code = 418
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.status_code == 418
    assert resp.json() == {"ok": True}


def test_handler_sets_header():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(response: Response):
        response.headers["X-Custom"] = "veloce"
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.headers.get("x-custom") == "veloce"


def test_handler_sets_cookie():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(response: Response):
        response.set_cookie("session", "abc123")
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert "session=abc123" in resp.headers.get("set-cookie", "")


def test_untouched_injected_response_leaves_status_200():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(response: Response):
        # Never mutate `response` — sentinel must not leak as the status.
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.status_code == 200


def test_handler_status_overrides_route_status_code():
    app = Veloce(openapi_url=None)

    @app.get("/x", status_code=201)
    async def x(response: Response):
        response.status_code = 202
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/x")

    # The handler-set status wins over the route declaration.
    assert resp.status_code == 202


def test_route_status_code_applies_when_handler_silent():
    app = Veloce(openapi_url=None)

    @app.get("/x", status_code=201)
    async def x(response: Response):
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.status_code == 201


def test_dependency_shares_same_response_object():
    app = Veloce(openapi_url=None)

    def stamp(response: Response):
        response.headers["X-From-Dep"] = "yes"
        return None

    @app.get("/x")
    async def x(response: Response, _=Depends(stamp)):
        response.headers["X-From-Handler"] = "yes"
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.headers.get("x-from-dep") == "yes"
    assert resp.headers.get("x-from-handler") == "yes"


def test_returned_response_ignores_injection():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(response: Response):
        response.status_code = 418
        # Returning a Response directly — that response wins, the
        # injected one is not merged.
        return Response(status_code=200, body=b"raw", content_type="text/plain")

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.status_code == 200
    assert resp.body == b"raw"
