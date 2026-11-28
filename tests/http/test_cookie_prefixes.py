"""RFC 6265bis Sec. 4.1.3 `__Host-`/`__Secure-` cookie-name prefixes."""

from __future__ import annotations

import pytest

from veloce import Request, Response, SessionMiddleware, Veloce
from veloce.http.cookies import dump_cookie


def _set_cookie(resp: Response) -> str:
    return resp.headers["Set-Cookie"]


# ── dump_cookie ──────────────────────────────────────────────────────


def test_dump_cookie_host_prefix_happy():
    out = dump_cookie("sess", "v", prefix="host", secure=True, path="/")
    assert out.startswith("__Host-sess=")
    assert "Secure" in out
    assert "Path=/" in out
    assert "Domain" not in out


def test_dump_cookie_secure_prefix_happy():
    out = dump_cookie("sess", "v", prefix="secure", secure=True)
    assert out.startswith("__Secure-sess=")
    assert "Secure" in out


def test_host_prefix_requires_secure():
    with pytest.raises(ValueError):
        dump_cookie("sess", "v", prefix="host", secure=False, path="/")


def test_host_prefix_rejects_path():
    with pytest.raises(ValueError):
        dump_cookie("sess", "v", prefix="host", secure=True, path="/admin")


def test_host_prefix_rejects_domain():
    with pytest.raises(ValueError):
        dump_cookie("sess", "v", prefix="host", secure=True, path="/", domain="example.com")


def test_secure_prefix_requires_secure():
    with pytest.raises(ValueError):
        dump_cookie("sess", "v", prefix="secure", secure=False)


def test_invalid_prefix_value():
    with pytest.raises(ValueError):
        dump_cookie("sess", "v", prefix="bogus", secure=True)  # type: ignore[arg-type]


# ── Response.set_cookie / delete_cookie ──────────────────────────────


def test_set_cookie_prefix_on_response():
    resp = Response()
    resp.set_cookie("id", "x", prefix="host", secure=True, path="/")
    assert _set_cookie(resp).startswith("__Host-id=")


def test_delete_cookie_prefix():
    resp = Response()
    resp.delete_cookie("id", prefix="secure", secure=True)
    cookie = _set_cookie(resp)
    assert cookie.startswith("__Secure-id=")
    assert "Max-Age=0" in cookie
    assert "Secure" in cookie


# ── Session middleware ───────────────────────────────────────────────


def test_session_middleware_host_prefix():
    app = Veloce(debug=False, openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, cookie_prefix="host", secure=True))

    @app.get("/write")
    async def write(request: Request):
        request.session["user"] = "alice"
        return {"ok": True}

    resp = app.test_client().get("/write")
    set_cookie = next(v for k, v in resp.headers.items() if k.lower() == "set-cookie")
    assert set_cookie.startswith("__Host-session=")


def test_session_middleware_host_prefix_requires_secure():
    with pytest.raises(ValueError):
        SessionMiddleware(secret_key="k" * 32, cookie_prefix="host", secure=False)


def test_session_middleware_host_prefix_round_trip():
    # The read side must look under the prefixed wire name or the session
    # never loads.
    app = Veloce(debug=False, openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, cookie_prefix="host", secure=True))

    @app.get("/set")
    async def set_it(request: Request):
        request.session["count"] = 7
        return {"ok": True}

    @app.get("/get")
    async def get_it(request: Request):
        return {"count": request.session.get("count")}

    client = app.test_client()
    client.get("/set")
    assert client.get("/get").json() == {"count": 7}
