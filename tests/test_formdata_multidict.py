"""FormData MultiDict semantics + is_json +json suffix (Q8, Q23)."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce
from veloce.http.datastructures import FormData, UploadFile
from veloce.testclient import TestClient

# ── Q8: FormData MultiDict construction ────────────────────────────────


def test_formdata_repeated_keys_preserved():
    fd = FormData([("tag", "a"), ("tag", "b"), ("tag", "c")])
    assert fd["tag"] == "a"  # first value wins for single-value access
    assert fd.getlist("tag") == ["a", "b", "c"]


def test_formdata_getlist_missing_returns_empty():
    fd = FormData([("a", "1")])
    assert fd.getlist("missing") == []


def test_formdata_get_upload_returns_first_uploadfile_only():
    """`get_upload` should return None for non-file fields, and the first
    UploadFile when multiple files share a key."""
    file_a = UploadFile(filename="a.txt")
    file_b = UploadFile(filename="b.txt")
    fd = FormData([("text", "x"), ("file", file_a), ("file", file_b)])
    assert fd.get_upload("text") is None
    upload = fd.get_upload("file")
    assert upload is file_a


# ── Q8: urlencoded form parsing preserves duplicates ──────────────────


@pytest.mark.asyncio
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


# ── Q8: multipart form parsing preserves duplicates ───────────────────


def _multipart_body(boundary: str, parts: list[tuple[str, str]]) -> bytes:
    """Build a minimal multipart body. parts = [(name, value), ...]."""
    lines = []
    for name, value in parts:
        lines.append(f"--{boundary}")
        lines.append(f'Content-Disposition: form-data; name="{name}"')
        lines.append("")
        lines.append(value)
    lines.append(f"--{boundary}--")
    lines.append("")
    return "\r\n".join(lines).encode()


@pytest.mark.asyncio
async def test_multipart_form_repeated_fields():
    boundary = "veloceboundary123"
    body = _multipart_body(
        boundary,
        [("tag", "a"), ("tag", "b"), ("name", "alice")],
    )
    req = Request(
        method="POST",
        path="/x",
        query_string="",
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        body=body,
    )
    form = await req.form()
    assert form.getlist("tag") == ["a", "b"]
    assert form["name"] == "alice"


# ── Q8: end-to-end via TestClient ─────────────────────────────────────


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


# ── Q23: is_json includes the +json structured suffix ─────────────────


def test_is_json_for_plain_application_json():
    req = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/json"},
        body=b"{}",
    )
    assert req.is_json is True


def test_is_json_for_structured_suffix():
    """RFC 6839 §3.1: `application/vnd.api+json` is JSON-encoded."""
    req = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/vnd.api+json"},
        body=b"{}",
    )
    assert req.is_json is True

    req2 = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/problem+json"},
        body=b"{}",
    )
    assert req2.is_json is True


def test_is_json_strips_charset_param():
    req = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/json; charset=utf-8"},
        body=b"{}",
    )
    assert req.is_json is True


def test_is_json_false_for_form_payloads():
    req = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/x-www-form-urlencoded"},
        body=b"a=1",
    )
    assert req.is_json is False


def test_is_json_false_for_text_plain():
    req = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "text/plain"},
        body=b"hi",
    )
    assert req.is_json is False


def test_is_json_false_for_missing_content_type():
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.is_json is False
