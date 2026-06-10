"""FileResponse content_disposition_type — inline vs attachment (the ASGI convention)."""

from __future__ import annotations

from pathlib import Path

import pytest

from veloce.http.response import FileResponse


@pytest.fixture
def sample_file(tmp_path: Path) -> str:
    f = tmp_path / "doc.txt"
    f.write_text("hello")
    return str(f)


def test_default_disposition_is_attachment(sample_file: str):
    resp = FileResponse(sample_file, filename="doc.txt")
    assert resp.headers["Content-Disposition"] == 'attachment; filename="doc.txt"'


def test_inline_disposition(sample_file: str):
    resp = FileResponse(sample_file, filename="doc.txt", content_disposition_type="inline")
    assert resp.headers["Content-Disposition"] == 'inline; filename="doc.txt"'


def test_no_disposition_without_filename(sample_file: str):
    resp = FileResponse(sample_file)
    assert "Content-Disposition" not in resp.headers


async def test_from_path_honours_disposition_type(sample_file: str):
    resp = await FileResponse.from_path(
        sample_file, filename="doc.txt", content_disposition_type="inline"
    )
    assert resp.headers["Content-Disposition"] == 'inline; filename="doc.txt"'


def test_inline_disposition_without_filename(sample_file: str):
    # An explicit non-default disposition is honoured even with no filename:
    # `Content-Disposition: inline` (a bare disposition is valid per RFC 6266).
    resp = FileResponse(sample_file, content_disposition_type="inline")
    assert resp.headers["Content-Disposition"] == "inline"


def test_default_attachment_without_filename_emits_no_header(sample_file: str):
    # The default `attachment` stays unset without a filename, so a plain
    # FileResponse does not force a download on every file it serves.
    resp = FileResponse(sample_file, content_disposition_type="attachment")
    assert "Content-Disposition" not in resp.headers
