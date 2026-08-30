"""Form parsing from the framework boundary — end-to-end via TestClient.

`MAX_FORM_PARTS`, Content-Disposition quoting, the charset fallback and
`FormData`'s duplicate handling, each driven through a real request rather
than by calling the parser directly.
"""

from __future__ import annotations

import pytest

from veloce import Request, Veloce
from veloce.http.datastructures import FormData
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


def test_urlencoded_form_field_cap_rejects_overflow():
    app = Veloce(openapi_url=None)
    app.config["MAX_FORM_PARTS"] = 3

    @app.post("/form")
    async def handle(request: Request):
        form = await request.form()
        return {"count": len(form)}

    with TestClient(app) as client:
        ok = client.post(
            "/form",
            content=b"a=1&b=2&c=3",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert ok.status_code == 200
        assert ok.json()["count"] == 3

        over = client.post(
            "/form",
            content=b"a=1&b=2&c=3&d=4",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert over.status_code == 413


def test_urlencoded_non_utf8_body_is_bad_request_not_413():
    """A non-UTF-8 urlencoded body is a malformed request (400), not a
    field-count overflow (413): `UnicodeDecodeError` subclasses `ValueError`,
    so the decode must sit outside the field-count guard."""
    app = Veloce(openapi_url=None)

    @app.post("/form")
    async def handle(request: Request):
        await request.form()
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.post(
            "/form",
            content=b"a=\xff\xfe",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 400


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


# ── `MAX_FORM_PARTS = None` means the same thing to both encodings ───
#
# The urlencoded branch read `None` as "no cap" - the comment beside it says so
# - while the multipart branch read it as "keep the built-in 1000", because the
# parser's `max_parts` was typed `int`. So one config value lifted the limit for
# one encoding and silently kept it for the other.

_BOUNDARY = "B0uNd"
_OVER_DEFAULT = 1500


def _form_client(**config) -> TestClient:
    app = Veloce(openapi_url=None)
    app.config.update(config)

    @app.post("/f")
    async def parse(request: Request):
        form = await request.form()
        return {"n": len(list(form.keys()))}

    return TestClient(app)


def _urlencoded(count: int) -> tuple[bytes, dict[str, str]]:
    body = "&".join(f"k{i}=v" for i in range(count)).encode()
    return body, {"content-type": "application/x-www-form-urlencoded"}


def _multipart(count: int) -> tuple[bytes, dict[str, str]]:
    body = (
        b"".join(
            f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="k{i}"\r\n\r\nv\r\n'.encode()
            for i in range(count)
        )
        + f"--{_BOUNDARY}--\r\n".encode()
    )
    return body, {"content-type": f"multipart/form-data; boundary={_BOUNDARY}"}


def test_a_none_cap_lifts_the_limit_for_a_multipart_body():
    """The defect: multipart kept the built-in 1000 whatever the config said."""
    body, headers = _multipart(_OVER_DEFAULT)
    response = _form_client(MAX_FORM_PARTS=None).post("/f", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["n"] == _OVER_DEFAULT


def test_a_none_cap_lifts_the_limit_for_a_urlencoded_body():
    body, headers = _urlencoded(_OVER_DEFAULT)
    response = _form_client(MAX_FORM_PARTS=None).post("/f", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["n"] == _OVER_DEFAULT


@pytest.mark.parametrize(
    "config",
    [
        pytest.param({"MAX_FORM_PARTS": None}, id="uncapped"),
        pytest.param({}, id="default"),
        pytest.param({"MAX_FORM_PARTS": 2000}, id="above-the-body"),
    ],
)
def test_both_encodings_answer_the_same_cap_the_same_way(config):
    """One config value, one meaning, whichever encoding was sent.

    Parametrized rather than looped so the report says *which* configuration
    disagreed. The three parses are the point and cost the same either way.
    """
    client = _form_client(**config)
    u_body, u_headers = _urlencoded(_OVER_DEFAULT)
    m_body, m_headers = _multipart(_OVER_DEFAULT)
    urlencoded = client.post("/f", content=u_body, headers=u_headers).status_code
    multipart = client.post("/f", content=m_body, headers=m_headers).status_code
    assert urlencoded == multipart


def test_the_default_cap_still_refuses_an_oversized_multipart_body():
    """Lifting the cap on request must not lift it by default."""
    body, headers = _multipart(_OVER_DEFAULT)
    assert _form_client().post("/f", content=body, headers=headers).status_code == 413


def test_an_explicit_cap_is_still_enforced_for_both():
    for build in (_urlencoded, _multipart):
        body, headers = build(20)
        response = _form_client(MAX_FORM_PARTS=5).post("/f", content=body, headers=headers)
        assert response.status_code == 413


# ── urlencoded form parsing preserves duplicates ───────────────
#
# Moved here from `test_formdata_multidict.py`, which covered three unrelated
# subsystems behind opaque tracker tags.


async def test_urlencoded_form_repeated_fields():
    req = Request(
        method="POST",
        path="/x",
        query_string="",
        headers={"content-type": "application/x-www-form-urlencoded"},
        body=b"tag=a&tag=b&tag=c&name=alice",
    )
    form = await req.form()
    assert isinstance(form, FormData)
    assert form.getlist("tag") == ["a", "b", "c"]
    assert form["name"] == "alice"
    assert form["tag"] == "a"  # first-value access


# ── duplicate form keys end to end ─────────────────────────────
#
# Moved here from `test_formdata_multidict.py`, which covered three unrelated
# subsystems behind opaque tracker tags.


def test_app_handler_sees_multiple_form_values():
    app = Veloce(debug=True, openapi_url=None)

    @app.post("/submit")
    async def submit(request: Request):
        form = await request.form()
        return {"tags": form.getlist("tag")}

    client = TestClient(app)
    # urlencoded
    resp = client.post(
        "/submit",
        content=b"tag=a&tag=b&tag=c",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.json() == {"tags": ["a", "b", "c"]}
