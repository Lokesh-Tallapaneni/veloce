"""StaticFiles conditional GET — ETag + Last-Modified + If-Modified-Since (ST3)."""

from __future__ import annotations

import os
from email.utils import formatdate

import pytest

from veloce import Veloce
from veloce.testclient import TestClient


@pytest.fixture
def static_app(tmp_path):
    """A veloce app mounted on /static pointing at tmp_path/."""
    asset = tmp_path / "data.txt"
    asset.write_bytes(b"hello world\n")

    # Set a known mtime so test assertions are reproducible.
    fixed_mtime = 1_700_000_000
    os.utime(str(asset), (fixed_mtime, fixed_mtime))

    app = Veloce(debug=True, openapi_url=None)
    app.mount_static(prefix="/static", directory=str(tmp_path))
    return app, fixed_mtime


def test_first_request_returns_200_with_etag_and_last_modified(static_app):
    app, mtime = static_app
    resp = TestClient(app).get("/static/data.txt")
    assert resp.status_code == 200
    assert resp.body == b"hello world\n"
    assert resp.headers.get("etag")
    last_modified = resp.headers.get("last-modified")
    assert last_modified
    # Header is IMF-fixdate per RFC 9110 §5.6.7 — should equal formatdate(mtime).
    assert last_modified == formatdate(mtime, usegmt=True)


def test_if_modified_since_matching_mtime_returns_304(static_app):
    app, mtime = static_app
    ims = formatdate(mtime, usegmt=True)
    resp = TestClient(app).get("/static/data.txt", headers={"if-modified-since": ims})
    assert resp.status_code == 304
    assert resp.body == b""
    # 304 still includes the ETag + Last-Modified per RFC 9110 §15.4.5.
    assert resp.headers.get("etag")
    assert resp.headers.get("last-modified")


def test_if_modified_since_in_the_past_returns_200(static_app):
    """Client's IMS is older than the file's mtime → server must send the body."""
    app, mtime = static_app
    older = formatdate(mtime - 3600, usegmt=True)
    resp = TestClient(app).get("/static/data.txt", headers={"if-modified-since": older})
    assert resp.status_code == 200
    assert resp.body == b"hello world\n"


def test_if_modified_since_in_the_future_returns_304(static_app):
    """Client claims to have a copy from after the mtime — 304."""
    app, mtime = static_app
    future = formatdate(mtime + 3600, usegmt=True)
    resp = TestClient(app).get("/static/data.txt", headers={"if-modified-since": future})
    assert resp.status_code == 304


def test_invalid_if_modified_since_header_ignored(static_app):
    """A malformed IMS header must not crash; should fall back to 200."""
    app, _ = static_app
    resp = TestClient(app).get("/static/data.txt", headers={"if-modified-since": "not a date"})
    assert resp.status_code == 200
    assert resp.body == b"hello world\n"


def test_if_none_match_supersedes_if_modified_since(static_app):
    """RFC 9110 §13.2 precedence: when both headers are present, INM wins.
    Send a non-matching INM with a matching IMS → must NOT 304 because INM
    fails the precondition."""
    app, mtime = static_app
    resp1 = TestClient(app).get("/static/data.txt")
    etag = resp1.headers["etag"]
    # Send a different ETag in INM and a matching IMS.
    resp2 = TestClient(app).get(
        "/static/data.txt",
        headers={
            "if-none-match": '"different"',
            "if-modified-since": formatdate(mtime, usegmt=True),
        },
    )
    # INM doesn't match → no 304, even though IMS would have triggered one.
    assert resp2.status_code == 200
    assert resp2.headers["etag"] == etag


def test_if_none_match_star_matches_any_etag(static_app):
    """`If-None-Match: *` matches any current representation → 304."""
    app, _ = static_app
    resp = TestClient(app).get("/static/data.txt", headers={"if-none-match": "*"})
    assert resp.status_code == 304


def test_if_none_match_matching_etag_returns_304(static_app):
    """Existing ETag behavior — preserved."""
    app, _ = static_app
    first = TestClient(app).get("/static/data.txt")
    etag = first.headers["etag"]

    resp = TestClient(app).get("/static/data.txt", headers={"if-none-match": etag})
    assert resp.status_code == 304


def test_if_none_match_strong_form_matches_weak_server_etag(static_app):
    """Client returns the opaque tag without the `W/` prefix.

    RFC 9110 §13.1.2 requires weak comparison for `If-None-Match`:
    opaque-equal tags must match regardless of the weak flag on either
    side. Intermediaries occasionally strip the `W/` prefix.
    """
    app, _ = static_app
    first = TestClient(app).get("/static/data.txt")
    server_etag = first.headers["etag"]
    # Server ETag is `W/"..."`. Strip the `W/` to produce the strong form.
    assert server_etag.startswith('W/"')
    strong_form = server_etag.removeprefix("W/")

    resp = TestClient(app).get("/static/data.txt", headers={"if-none-match": strong_form})
    assert resp.status_code == 304
    assert resp.body == b""


def test_if_none_match_weak_form_matches_weak_server_etag(static_app):
    """Client echoes the server's `W/"..."` tag verbatim — must 304."""
    app, _ = static_app
    first = TestClient(app).get("/static/data.txt")
    server_etag = first.headers["etag"]
    assert server_etag.startswith('W/"')

    resp = TestClient(app).get("/static/data.txt", headers={"if-none-match": server_etag})
    assert resp.status_code == 304
    assert resp.body == b""


def test_if_none_match_mismatched_tag_returns_200(static_app):
    """A tag that does not match must serve the body with 200."""
    app, _ = static_app
    resp = TestClient(app).get(
        "/static/data.txt", headers={"if-none-match": '"completely-different"'}
    )
    assert resp.status_code == 200
    assert resp.body == b"hello world\n"
