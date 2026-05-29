"""End-to-end tests for request props (#9, #27, #28) and form parser (#51).

Exercises each fix from the framework boundary via TestClient.
"""

from __future__ import annotations

from veloce import Request, Veloce
from veloce.testclient import TestClient


def test_max_form_parts_zero_rejects_any_part():
    app = Veloce(openapi_url=None)
    app.config["MAX_FORM_PARTS"] = 0

    @app.post("/upload")
    async def upload(request: Request):
        await request.form()
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.post("/upload", files={"f": ("a.txt", b"hello", "text/plain")})
    assert resp.status_code == 413


def test_max_form_parts_five_allows_five_rejects_six():
    app = Veloce(openapi_url=None)
    app.config["MAX_FORM_PARTS"] = 5

    @app.post("/upload")
    async def upload(request: Request):
        form = await request.form()
        return {"count": len(form)}

    with TestClient(app) as client:
        five = {f"f{i}": (f"a{i}.txt", b"x", "text/plain") for i in range(5)}
        resp = client.post("/upload", files=five)
        assert resp.status_code == 200

        six = {f"f{i}": (f"a{i}.txt", b"x", "text/plain") for i in range(6)}
        resp = client.post("/upload", files=six)
        assert resp.status_code == 413


def test_auth_property_cached_across_repeat_access():
    app = Veloce(openapi_url=None)
    observed = {}

    @app.get("/auth")
    async def auth_route(request: Request):
        a1 = request.auth
        a2 = request.auth
        observed["same"] = a1 is a2
        observed["scheme"] = a1.type if a1 else None
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/auth", headers={"Authorization": "Bearer abc"})
    assert observed["same"] is True
    assert observed["scheme"] == "bearer"


def test_range_property_cached_across_repeat_access():
    app = Veloce(openapi_url=None)
    observed = {}

    @app.get("/range")
    async def range_route(request: Request):
        r1 = request.range
        r2 = request.range
        observed["same"] = r1 is r2
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/range", headers={"Range": "bytes=0-100"})
    assert observed["same"] is True


def test_form_parser_quoted_semicolon_in_name():
    app = Veloce(openapi_url=None)
    observed = {}

    @app.post("/p")
    async def p(request: Request):
        form = await request.form()
        observed["keys"] = list(form.keys())
        return {"ok": True}

    body = b'--BOUND\r\nContent-Disposition: form-data; name="a;b"\r\n\r\nhello\r\n--BOUND--\r\n'
    with TestClient(app) as client:
        resp = client.post(
            "/p",
            content=body,
            headers={"Content-Type": "multipart/form-data; boundary=BOUND"},
        )
    assert resp.status_code == 200
    assert "a;b" in observed["keys"]


def test_form_parser_escaped_quote_in_filename():
    app = Veloce(openapi_url=None)
    observed = {}

    @app.post("/p")
    async def p(request: Request):
        form = await request.form()
        for key, value in form.items():
            filename = getattr(value, "filename", None)
            if filename:
                observed.setdefault("filenames", []).append(filename)
        return {"ok": True}

    body = (
        b"--BOUND\r\n"
        b'Content-Disposition: form-data; name="upload"; filename="x\\"y.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
        b"content\r\n"
        b"--BOUND--\r\n"
    )
    with TestClient(app) as client:
        resp = client.post(
            "/p",
            content=body,
            headers={"Content-Type": "multipart/form-data; boundary=BOUND"},
        )
    assert resp.status_code == 200
    assert observed.get("filenames") == ['x"y.txt']


def test_form_parser_unquoted_value():
    app = Veloce(openapi_url=None)
    observed = {}

    @app.post("/p")
    async def p(request: Request):
        form = await request.form()
        observed["keys"] = list(form.keys())
        return {"ok": True}

    body = b"--BOUND\r\nContent-Disposition: form-data; name=plain\r\n\r\nhello\r\n--BOUND--\r\n"
    with TestClient(app) as client:
        resp = client.post(
            "/p",
            content=body,
            headers={"Content-Type": "multipart/form-data; boundary=BOUND"},
        )
    assert resp.status_code == 200
    assert "plain" in observed["keys"]
