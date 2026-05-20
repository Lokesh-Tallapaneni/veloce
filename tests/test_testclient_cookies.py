"""TestClient.cookies + set_cookie + delete_cookie."""

from __future__ import annotations

from veloce import Request, Response, Veloce


def _make_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/whoami")
    async def whoami(request: Request):
        return {"sid": request.cookies.get("sid", ""), "lang": request.cookies.get("lang", "")}

    @app.get("/setit")
    async def setit():
        resp = Response(body=b"ok")
        resp.set_cookie("server_set", "value-from-server")
        return resp

    return app


def test_set_cookie_then_get_back():
    app = _make_app()
    client = app.test_client()
    client.set_cookie("sid", "abc123")
    resp = client.get("/whoami")
    assert resp.json() == {"sid": "abc123", "lang": ""}


def test_cookies_dict_like_setitem():
    app = _make_app()
    client = app.test_client()
    client.cookies["sid"] = "xyz"
    client.cookies["lang"] = "en"
    resp = client.get("/whoami")
    assert resp.json() == {"sid": "xyz", "lang": "en"}


def test_cookies_iteration_and_len():
    app = _make_app()
    client = app.test_client()
    client.set_cookie("a", "1")
    client.set_cookie("b", "2")
    assert len(client.cookies) == 2
    assert sorted(client.cookies) == ["a", "b"]
    assert "a" in client.cookies
    assert client.cookies.get("a") == "1"


def test_delete_cookie_removes_entry():
    app = _make_app()
    client = app.test_client()
    client.set_cookie("sid", "abc")
    assert "sid" in client.cookies
    client.delete_cookie("sid")
    assert "sid" not in client.cookies
    # Deleting a missing cookie is a no-op.
    client.delete_cookie("nope")


def test_cookies_clear_wipes_all():
    app = _make_app()
    client = app.test_client()
    client.set_cookie("a", "1")
    client.set_cookie("b", "2")
    client.cookies.clear()
    assert len(client.cookies) == 0


def test_server_set_cookie_lands_in_jar():
    app = _make_app()
    client = app.test_client()
    client.get("/setit")
    assert client.cookies["server_set"] == "value-from-server"
    # Next request includes it automatically.
    resp = client.get("/whoami")
    # `server_set` doesn't affect /whoami's response, but the cookie
    # made it into the jar (verified above) and would be sent.
    assert resp.status_code == 200
