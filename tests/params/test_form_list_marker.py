"""Form()-marked list parameters collect every repeated field value."""

from __future__ import annotations

from veloce import Form, Veloce
from veloce.testclient import TestClient


def test_form_list_collects_multiple_values():
    app = Veloce(openapi_url=None)

    @app.post("/submit")
    async def submit(tags: list[str] = Form(default=[])):
        return {"tags": tags}

    with TestClient(app) as client:
        resp = client.post("/submit", data=[("tags", "a"), ("tags", "b")])

    assert resp.json() == {"tags": ["a", "b"]}


def test_form_list_single_value():
    app = Veloce(openapi_url=None)

    @app.post("/submit")
    async def submit(tags: list[str] = Form(default=[])):
        return {"tags": tags}

    with TestClient(app) as client:
        resp = client.post("/submit", data={"tags": "solo"})

    assert resp.json() == {"tags": ["solo"]}


def test_form_list_default_when_absent():
    app = Veloce(openapi_url=None)

    @app.post("/submit")
    async def submit(tags: list[str] = Form(default=["none"])):
        return {"tags": tags}

    with TestClient(app) as client:
        resp = client.post("/submit", data={"other": "x"})

    assert resp.json() == {"tags": ["none"]}


def test_form_list_int_items_coerced():
    app = Veloce(openapi_url=None)

    @app.post("/submit")
    async def submit(nums: list[int] = Form(default=[])):
        return {"sum": sum(nums)}

    with TestClient(app) as client:
        resp = client.post("/submit", data=[("nums", "2"), ("nums", "3"), ("nums", "5")])

    assert resp.json() == {"sum": 10}


def test_form_list_required_missing_is_422():
    app = Veloce(openapi_url=None)

    @app.post("/submit")
    async def submit(tags: list[str] = Form()):
        return {"tags": tags}

    with TestClient(app) as client:
        resp = client.post("/submit", data={"other": "x"})

    assert resp.status_code == 422
