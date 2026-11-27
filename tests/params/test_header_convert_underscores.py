"""Header(convert_underscores=...) — underscore→hyphen mapping."""

from __future__ import annotations

from veloce import Header, Veloce
from veloce.testclient import TestClient


def test_header_param_auto_converts_underscores():
    """`x_token` param matches the `X-Token` request header by default."""
    app = Veloce()

    @app.get("/a")
    async def a(x_token: str = Header()):
        return {"token": x_token}

    with TestClient(app) as client:
        resp = client.get("/a", headers={"X-Token": "secret"})
        assert resp.status_code == 200
        assert resp.json()["token"] == "secret"


def test_header_convert_underscores_false_keeps_underscore():
    app = Veloce()

    @app.get("/b")
    async def b(weird_name: str = Header(default="", convert_underscores=False)):
        return {"v": weird_name}

    with TestClient(app) as client:
        # With conversion off, only a literal `weird_name` header matches.
        resp = client.get("/b", headers={"weird_name": "matched"})
        assert resp.json()["v"] == "matched"
        # The hyphenated form does NOT match.
        resp2 = client.get("/b", headers={"weird-name": "nope"})
        assert resp2.json()["v"] == ""


def test_explicit_alias_overrides_conversion():
    app = Veloce()

    @app.get("/c")
    async def c(tok: str = Header(alias="Authorization")):
        return {"v": tok}

    with TestClient(app) as client:
        resp = client.get("/c", headers={"Authorization": "Bearer xyz"})
        assert resp.json()["v"] == "Bearer xyz"


def test_convert_underscores_attribute_default_true():
    assert Header().convert_underscores is True


def test_convert_underscores_attribute_set_false():
    assert Header(convert_underscores=False).convert_underscores is False
