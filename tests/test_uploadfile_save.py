"""UploadFile.save tests (Q9 partial)."""

from __future__ import annotations

import io
from pathlib import Path

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
    import tempfile

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
