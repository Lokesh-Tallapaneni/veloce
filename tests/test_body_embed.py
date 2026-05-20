"""Body(embed=True) — single-body-param nesting."""

from __future__ import annotations

from veloce import Body, Veloce
from veloce.testclient import TestClient


def test_body_without_embed_uses_whole_body():
    app = Veloce()

    @app.post("/plain")
    async def plain(payload: dict = Body()):
        return {"got": payload}

    with TestClient(app) as client:
        resp = client.post("/plain", json={"name": "alice"})
        assert resp.status_code == 200
        assert resp.json()["got"] == {"name": "alice"}


def test_body_embed_nests_under_param_name():
    app = Veloce()

    @app.post("/embedded")
    async def embedded(item: dict = Body(embed=True)):
        return {"got": item}

    with TestClient(app) as client:
        # With embed=True, the value lives under the "item" key.
        resp = client.post("/embedded", json={"item": {"name": "bob"}})
        assert resp.status_code == 200
        assert resp.json()["got"] == {"name": "bob"}


def test_body_embed_missing_key_uses_default():
    app = Veloce()

    @app.post("/opt")
    async def opt(item: dict = Body(default={"fallback": True}, embed=True)):
        return {"got": item}

    with TestClient(app) as client:
        # Body present but no "item" key → default.
        resp = client.post("/opt", json={"other": 1})
        assert resp.json()["got"] == {"fallback": True}


def test_body_embed_attribute_defaults_false():
    assert Body().embed is False


def test_body_embed_attribute_set():
    assert Body(embed=True).embed is True
