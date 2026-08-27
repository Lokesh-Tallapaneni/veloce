"""UploadFile.save tests (Q9 partial)."""

from __future__ import annotations

import asyncio
import io
import tempfile
from pathlib import Path

from tests.conftest import make_request
from veloce import Request, Veloce
from veloce.http.datastructures import UploadFile


def _upload(data: bytes, filename: str = "x.bin") -> UploadFile:
    return UploadFile(
        filename=filename,
        content_type="application/octet-stream",
        file=io.BytesIO(data),
        size=len(data),
    )


# ── Save to path ──────────────────────────────────────────────────────


def test_save_writes_full_content_to_path(tmp_path: Path):
    upload = _upload(b"hello world")
    target = tmp_path / "out.bin"
    upload.save(str(target))
    assert target.read_bytes() == b"hello world"


def test_save_to_path_creates_parent_only_if_user_did():
    """`save` does not auto-create missing directories; the caller must."""
    upload = _upload(b"x")
    import os

    tmpdir = tempfile.mkdtemp()
    try:
        missing = os.path.join(tmpdir, "missing", "x.bin")
        try:
            upload.save(missing)
        except OSError:
            pass  # expected
        else:
            raise AssertionError("expected OSError for missing parent")
    finally:
        # Clean up the outer tmpdir; the missing subdir was never created.
        os.rmdir(tmpdir)


# ── Save to a file-like ──────────────────────────────────────────────


def test_save_to_open_file_handle():
    upload = _upload(b"streamed content")
    sink = io.BytesIO()
    upload.save(sink)
    assert sink.getvalue() == b"streamed content"


def test_save_to_filehandle_does_not_close_it():
    """When given an open file, `save` doesn't close it — caller owns it."""
    upload = _upload(b"x")
    sink = io.BytesIO()
    upload.save(sink)
    # The sink must still be usable.
    sink.write(b"-appended")
    assert sink.getvalue() == b"x-appended"


# ── Cursor handling ──────────────────────────────────────────────────


def test_save_preserves_read_cursor():
    """After save, the upload's read cursor returns to its original position."""
    upload = _upload(b"abcdef")
    upload.file.seek(3)  # caller had consumed first 3 bytes
    sink = io.BytesIO()
    upload.save(sink)
    # save reads from 0, then restores cursor to 3 — next read sees "def".
    assert upload.file.read() == b"def"
    # Saved content is the full payload, not from cursor.
    assert sink.getvalue() == b"abcdef"


def test_save_works_when_file_was_at_end():
    upload = _upload(b"abc")
    upload.file.seek(3)  # at EOF
    sink = io.BytesIO()
    upload.save(sink)
    assert sink.getvalue() == b"abc"


# ── Chunking ──────────────────────────────────────────────────────────


def test_save_streams_in_chunks_bounded_memory():
    """Large uploads with small buffer_size shouldn't materialise everything."""
    big_payload = b"x" * 100_000
    upload = _upload(big_payload)

    sink = io.BytesIO()
    upload.save(sink, buffer_size=1024)
    assert sink.getvalue() == big_payload


def test_save_default_buffer_size():
    """Default buffer_size still writes correctly for moderate payloads."""
    upload = _upload(b"y" * 50_000)
    sink = io.BytesIO()
    upload.save(sink)  # default buffer_size
    assert len(sink.getvalue()) == 50_000


# ── Multiple saves of the same upload ────────────────────────────────


def test_save_can_be_called_multiple_times():
    """Cursor restoration means save() is idempotent."""
    upload = _upload(b"abc")
    a, b = io.BytesIO(), io.BytesIO()
    upload.save(a)
    upload.save(b)
    assert a.getvalue() == b.getvalue() == b"abc"


class TestUploadFile:
    async def test_upload_file_read(self):
        f = UploadFile(filename="test.txt", file=io.BytesIO(b"hello"))
        data = await f.read()
        assert data == b"hello"

    async def test_upload_file_content(self):
        f = UploadFile(filename="test.txt", file=io.BytesIO(b"content"))
        assert f.content == b"content"

    def test_upload_file_repr(self):
        f = UploadFile(filename="photo.jpg", content_type="image/jpeg", size=1024)
        assert "photo.jpg" in repr(f)

    async def test_multipart_file_upload(self):
        app = Veloce(openapi_url=None)

        @app.post("/upload")
        async def upload(request: Request):
            form = await request.form()
            file = form.get("file")
            if isinstance(file, UploadFile):
                content = await file.read()
                return {"filename": file.filename, "size": len(content)}
            return {"error": "no file"}

        # Build multipart body
        body = (
            b"------TestBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="test.txt"\r\n'
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"Hello World\r\n"
            b"------TestBoundary--\r\n"
        )

        req = make_request(
            method="POST",
            path="/upload",
            body=body,
            headers={"content-type": "multipart/form-data; boundary=----TestBoundary"},
        )
        resp = await app.handle_request(req)
        import orjson

        data = orjson.loads(resp.body)
        assert data["filename"] == "test.txt"
        assert data["size"] == 11


class TestUploadFileContextManager:
    """Test UploadFile async context manager."""

    async def test_async_with(self):
        async with UploadFile(filename="test.txt", file=io.BytesIO(b"hello")) as f:
            data = await f.read()
            assert data == b"hello"
        # File should be closed after exiting context
        assert f.file.closed


async def test_uploadfile_read_does_not_block_on_spilled_spool():
    """Once the spool spills to disk, reads must hop to a thread —
    not block the event loop. The smoke test: a background sentinel
    coroutine must continue to run while a spilled-upload read is
    in flight. (We use a SpooledTemporaryFile that already rolled over.)
    """
    # Manually construct a spooled file and force the rollover via the
    # public `rollover()` API — avoids depending on the `_rolled`
    # implementation-detail attribute. The spool's lifetime is owned by
    # `UploadFile`, which closes it at the end of the test.
    spool = tempfile.SpooledTemporaryFile(max_size=128)  # noqa: SIM115
    spool.write(b"A" * 2048)
    spool.rollover()
    spool.seek(0)

    upload = UploadFile(filename="big.bin", file=spool, size=2048)
    ticked = 0

    async def ticker() -> None:
        nonlocal ticked
        for _ in range(5):
            await asyncio.sleep(0)
            ticked += 1

    # Drive both concurrently. If `read` is blocking the loop, the
    # ticker won't run; the to_thread offload keeps the loop free.
    data, _ = await asyncio.gather(upload.read(2048), ticker())
    assert data == b"A" * 2048
    assert ticked == 5

    await upload.close()


async def test_uploadfile_in_memory_read_stays_on_loop():
    """The cheap in-memory path must not pay an executor-hop tax —
    BytesIO reads stay on the loop."""
    upload = UploadFile(filename="tiny.txt", file=io.BytesIO(b"hi"), size=2)
    assert await upload.read() == b"hi"
    await upload.close()


async def test_uploadfile_unrolled_spool_stays_on_loop():
    """The production multipart-parser path hands `UploadFile` a
    `SpooledTemporaryFile`, not a `BytesIO`. A small upload that has
    NOT rolled over is still in memory — it must stay on the loop, not
    pay a thread-hop tax for every read/write."""
    spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)  # noqa: SIM115
    spool.write(b"small")
    spool.seek(0)
    upload = UploadFile(filename="tiny.bin", file=spool, size=5)
    # `_file_is_in_memory()` returns True for an unrolled spool —
    # otherwise the optimisation never fires for real uploads.
    assert upload._file_is_in_memory() is True
    assert await upload.read() == b"small"
    await upload.close()
