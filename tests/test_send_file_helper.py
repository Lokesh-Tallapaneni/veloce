"""send_file() / async_send_file() top-level helpers."""

from __future__ import annotations

import asyncio
import datetime as dt

from veloce import FileResponse, Request, Veloce, async_send_file, send_file


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


def test_no_cache_control_outside_request_context(tmp_path):
    """Without an active app there is no config to consult - same as before."""
    resp = send_file(_make_file(tmp_path))
    assert "Cache-Control" not in resp.headers


async def test_max_age_default_from_config(tmp_path):
    """`SEND_FILE_MAX_AGE_DEFAULT` supplies Cache-Control when the caller
    does not pass `max_age=` and a request is in flight."""

    app = Veloce(openapi_url=None)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 1234
    path = _make_file(tmp_path)

    @app.get("/f")
    async def f():
        return await async_send_file(path)

    req = Request(method="GET", path="/f", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert resp.headers["Cache-Control"] == "public, max-age=1234"


async def test_explicit_max_age_beats_config_default(tmp_path):

    app = Veloce(openapi_url=None)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 1234
    path = _make_file(tmp_path)

    @app.get("/f")
    async def f():
        return await async_send_file(path, max_age=60)

    req = Request(method="GET", path="/f", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert resp.headers["Cache-Control"] == "public, max-age=60"


async def test_async_returns_file_response(tmp_path):
    resp = await async_send_file(_make_file(tmp_path))
    assert isinstance(resp, FileResponse)
    assert resp.body == b"hello"


async def test_async_matches_sync_headers(tmp_path):
    path = _make_file(tmp_path, "report.pdf")
    # Compute the sync reference off the running loop so `send_file` does not
    # emit its on-loop DeprecationWarning; the comparison checks that
    # `async_send_file` builds the same headers as `send_file`.
    sync_resp = await asyncio.to_thread(
        send_file, path, as_attachment=True, download_name="r.pdf", max_age=3600
    )
    async_resp = await async_send_file(
        path, as_attachment=True, download_name="r.pdf", max_age=3600
    )
    assert async_resp.headers["Content-Disposition"] == sync_resp.headers["Content-Disposition"]
    assert async_resp.headers["Cache-Control"] == sync_resp.headers["Cache-Control"]
    assert async_resp.content_type == sync_resp.content_type


async def test_async_etag_false_strips_header(tmp_path):
    resp = await async_send_file(_make_file(tmp_path), etag=False)
    assert "ETag" not in resp.headers
    assert "Last-Modified" in resp.headers


async def test_async_last_modified_override_datetime(tmp_path):
    resp = await async_send_file(
        _make_file(tmp_path),
        last_modified=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
    )
    assert "01 Jan 2030" in resp.headers["Last-Modified"]
