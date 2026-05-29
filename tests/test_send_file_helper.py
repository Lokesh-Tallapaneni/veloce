"""send_file() top-level helper."""

from __future__ import annotations

import datetime as dt

from veloce import FileResponse, send_file


def _make_file(tmp_path, name: str = "a.txt", content: bytes = b"hello") -> str:
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


def test_returns_file_response(tmp_path):
    resp = send_file(_make_file(tmp_path))
    assert isinstance(resp, FileResponse)
    assert resp.body == b"hello"


def test_default_emits_last_modified_and_etag(tmp_path):
    resp = send_file(_make_file(tmp_path))
    assert "Last-Modified" in resp.headers
    assert resp.headers["ETag"].startswith('W/"')


def test_mimetype_override(tmp_path):
    resp = send_file(_make_file(tmp_path, "foo.unknown"), mimetype="application/x-custom")
    assert resp.content_type == "application/x-custom"


def test_as_attachment_sets_content_disposition(tmp_path):
    resp = send_file(_make_file(tmp_path, "report.pdf"), as_attachment=True, download_name="r.pdf")
    cd = resp.headers["Content-Disposition"]
    assert "attachment" in cd
    assert 'filename="r.pdf"' in cd


def test_last_modified_override_datetime(tmp_path):
    resp = send_file(
        _make_file(tmp_path),
        last_modified=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
    )
    assert "01 Jan 2030" in resp.headers["Last-Modified"]


def test_etag_false_strips_header(tmp_path):
    resp = send_file(_make_file(tmp_path), etag=False)
    assert "ETag" not in resp.headers
    assert "Last-Modified" in resp.headers  # other headers untouched


def test_etag_string_used_verbatim(tmp_path):
    resp = send_file(_make_file(tmp_path), etag='"custom-tag"')
    assert resp.headers["ETag"] == '"custom-tag"'


def test_max_age_sets_cache_control(tmp_path):
    resp = send_file(_make_file(tmp_path), max_age=3600)
    assert resp.headers["Cache-Control"] == "public, max-age=3600"
