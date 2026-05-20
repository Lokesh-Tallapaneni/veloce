"""TestClient.post(files=...) multipart upload support."""

from __future__ import annotations

import pytest

from veloce import Request, UploadFile, Veloce


def _echo_app() -> Veloce:
    """App that echoes the file upload back."""
    app = Veloce(debug=True, openapi_url=None)

    @app.post("/upload")
    async def upload(request: Request):
        form = await request.form()
        uploaded = form.get("file")
        if isinstance(uploaded, UploadFile):
            content = await uploaded.read()
            return {
                "filename": uploaded.filename,
                "content_type": uploaded.content_type,
                "size": len(content),
                "body": content.decode("utf-8"),
            }
        return {"err": "no file"}

    return app


def test_post_files_simple_bytes():
    app = _echo_app()
    client = app.test_client()
    resp = client.post("/upload", files={"file": b"hello world"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"] == "file"
    assert body["body"] == "hello world"


def test_post_files_two_tuple():
    app = _echo_app()
    client = app.test_client()
    resp = client.post(
        "/upload",
        files={"file": ("hello.txt", b"hi from tuple")},
    )
    body = resp.json()
    assert body["filename"] == "hello.txt"
    assert body["body"] == "hi from tuple"


def test_post_files_three_tuple_with_content_type():
    app = _echo_app()
    client = app.test_client()
    resp = client.post(
        "/upload",
        files={"file": ("data.json", b'{"k":1}', "application/json")},
    )
    body = resp.json()
    assert body["filename"] == "data.json"
    assert body["content_type"] == "application/json"


def test_post_files_with_extra_form_fields():
    """Mixing `data=` form fields and `files=` works."""
    app = Veloce(debug=True, openapi_url=None)

    @app.post("/x")
    async def x(request: Request):
        form = await request.form()
        return {
            "username": form.get("username"),
            "filename": form["file"].filename if "file" in form else None,
        }

    client = app.test_client()
    resp = client.post(
        "/x",
        data={"username": "alice"},
        files={"file": ("avatar.png", b"PNG-bytes")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "alice"
    assert body["filename"] == "avatar.png"


def test_post_files_invalid_spec_raises():
    """Passing junk like an int falls through to a clear error."""
    app = _echo_app()
    client = app.test_client()
    with pytest.raises(TypeError, match="files\\['file'\\]"):
        client.post("/upload", files={"file": 12345})  # type: ignore[dict-item]
