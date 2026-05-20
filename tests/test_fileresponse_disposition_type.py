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
