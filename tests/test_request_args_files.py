"""Request.args / Request.files aliases."""

from __future__ import annotations

import pytest

from veloce import Request


def _req(query: str = "", body: bytes = b"", content_type: str = "") -> Request:
    headers = {"content-type": content_type} if content_type else {}
    return Request(method="GET", path="/", query_string=query, headers=headers, body=body)


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
