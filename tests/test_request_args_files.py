"""Request.args / Request.files aliases."""

from __future__ import annotations

import pytest

from veloce import FilesKeyError, Request
from veloce.http.datastructures import FormData, UploadFile


class _AppStub:
    """Minimal app stand-in exposing the fields `Request.files` reads."""

    def __init__(self, debug: bool) -> None:
        self.debug = debug
        self.config: dict[str, object] = {}


def _req(
    query: str = "",
    body: bytes = b"",
    content_type: str = "",
    app: object | None = None,
) -> Request:
    headers = {"content-type": content_type} if content_type else {}
    return Request(
        method="GET",
        path="/",
        query_string=query,
        headers=headers,
        body=body,
        app=app,
    )


# ── Request.args ────────────────────────────────────────────────────


def test_args_is_query_params():
    req = _req(query="a=1&b=2")
    assert req.args is req.query_params


def test_args_reads_query_values():
    req = _req(query="name=alice&age=30")
    assert req.args["name"] == "alice"
    assert req.args["age"] == "30"


def test_args_empty_when_no_query():
    req = _req()
    assert len(req.args) == 0


def test_args_preserves_duplicate_keys():
    req = _req(query="tag=x&tag=y")
    assert req.args.getlist("tag") == ["x", "y"]


# ── Request.files ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_files_empty_for_non_multipart():
    req = _req()
    files = await req.files()
    assert len(files) == 0


@pytest.mark.asyncio
async def test_files_empty_for_urlencoded_form():
    req = _req(
        body=b"a=1&b=2",
        content_type="application/x-www-form-urlencoded",
    )
    files = await req.files()
    # urlencoded form has no file uploads.
    assert len(files) == 0


@pytest.mark.asyncio
async def test_files_extracts_uploads_from_multipart():
    boundary = "----testbound"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="doc"; filename="a.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "hello\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="title"\r\n\r\n'
        "My Title\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    req = _req(
        body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    files = await req.files()
    # Only the file part — the plain "title" field is excluded.
    assert "doc" in files
    assert "title" not in files


@pytest.mark.asyncio
async def test_files_handles_multiple_uploads_under_one_field_name():
    """Several files sharing one field name must yield exactly that many
    entries — not N×N duplicates from re-`getlist`-ing each repeated key."""
    boundary = "----testbound"
    parts = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="doc"; filename="f{i}.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        f"contents-{i}\r\n"
        for i in range(4)
    ]
    body = ("".join(parts) + f"--{boundary}--\r\n").encode()
    req = _req(body=body, content_type=f"multipart/form-data; boundary={boundary}")

    files = await req.files()
    # Four uploads in, four entries out — no N×N duplication.
    assert len(files) == 4
    docs = files.getlist("doc")
    assert len(docs) == 4
    assert sorted(d.filename for d in docs) == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]


# ── Request.files debug-mode missing-key diagnostics ────────────────


@pytest.mark.asyncio
async def test_files_missing_key_is_bare_keyerror_without_debug():
    req = _req(
        body=b"avatar=oops",
        content_type="application/x-www-form-urlencoded",
        app=_AppStub(debug=False),
    )
    files = await req.files()
    with pytest.raises(KeyError) as exc:
        files["avatar"]
    # No app debug → plain multidict KeyError, message is just the key repr.
    assert not isinstance(exc.value, FilesKeyError)
    assert str(exc.value) == "'avatar'"


@pytest.mark.asyncio
async def test_files_missing_key_hints_enctype_for_plain_form_field():
    req = _req(
        body=b"avatar=oops",
        content_type="application/x-www-form-urlencoded",
        app=_AppStub(debug=True),
    )
    files = await req.files()
    with pytest.raises(FilesKeyError) as exc:
        files["avatar"]
    # FilesKeyError is a KeyError subclass, so existing handlers still catch it.
    assert isinstance(exc.value, KeyError)
    msg = str(exc.value)
    assert "avatar" in msg
    assert 'enctype="multipart/form-data"' in msg


@pytest.mark.asyncio
async def test_files_missing_key_hints_json_body():
    req = _req(
        body=b'{"avatar": "x"}',
        content_type="application/json",
        app=_AppStub(debug=True),
    )
    files = await req.files()
    with pytest.raises(FilesKeyError) as exc:
        files["avatar"]
    assert "JSON request" in str(exc.value)


@pytest.mark.asyncio
async def test_files_missing_key_hints_no_multipart_body():
    req = _req(app=_AppStub(debug=True))
    files = await req.files()
    with pytest.raises(FilesKeyError) as exc:
        files["missing"]
    assert "multipart/form-data" in str(exc.value)


@pytest.mark.asyncio
async def test_files_present_key_returns_upload_in_debug_mode():
    boundary = "----testbound"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="doc"; filename="a.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "hello\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    req = _req(
        body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
        app=_AppStub(debug=True),
    )
    files = await req.files()
    # Success path is unchanged: a present key returns the upload, no error.
    assert files["doc"].filename == "a.txt"


# ── FormData MultiDict construction ────────────────────────────
#
# Moved here from `test_formdata_multidict.py`, which covered three unrelated
# subsystems behind opaque tracker tags.


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
