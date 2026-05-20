"""app.add_middleware(Class, **options) — imperative form."""

from __future__ import annotations

from veloce import CORSMiddleware, Veloce
from veloce.testclient import TestClient


def test_add_middleware_class_form_instantiates():
    app = Veloce()
    app.add_middleware(CORSMiddleware, allow_origins=["https://example.com"])
    # One middleware instance appended.
    assert len(app._middlewares) == 1
    assert isinstance(app._middlewares[0], CORSMiddleware)


def test_add_middleware_instance_form_still_works():
    app = Veloce()
    instance = CORSMiddleware(allow_origins=["*"])
    app.add_middleware(instance)
    assert app._middlewares[-1] is instance


def test_add_middleware_class_form_applies_options():
    app = Veloce()
    app.add_middleware(CORSMiddleware, allow_origins=["https://allowed.test"])

    @app.get("/x")
    async def x():
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/x", headers={"Origin": "https://allowed.test"})
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "https://allowed.test"


def test_add_middleware_multiple():
    app = Veloce()
    app.add_middleware(CORSMiddleware, allow_origins=["*"])
    app.add_middleware(CORSMiddleware, allow_origins=["*"])
    assert len(app._middlewares) == 2
