"""StaticFiles write-side preconditions — If-Match / If-Unmodified-Since (412)."""

from __future__ import annotations

import os

import pytest

from tests.conftest import make_request
from veloce import Request
from veloce.contrib.staticfiles import StaticFiles
from veloce.http.dates import http_date


def _req(path: str, headers: dict | None = None) -> Request:
    return make_request(
        method="GET",
        path=path,
        query_string="",
        headers=headers or {},
        body=b"",
    )


@pytest.fixture()
def static(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"0123456789" * 10)  # 100 bytes
    return StaticFiles(directory=str(tmp_path), prefix="/static"), str(f)


# ── If-Match (weak file ETags fail closed; only `*` succeeds) ─────────


async def test_if_match_concrete_tag_returns_412(static):
    sf, _ = static
    # First fetch the file's own (weak) ETag, then send it back as If-Match.
    base = await sf.handle(_req("/static/blob.bin"))
    resp = await sf.handle(_req("/static/blob.bin", {"if-match": base.headers["ETag"]}))
    assert resp.status_code == 412
    assert resp.body == b""
    assert "ETag" in resp.headers and "Last-Modified" in resp.headers


async def test_if_match_wildcard_returns_200(static):
    sf, _ = static
    resp = await sf.handle(_req("/static/blob.bin", {"if-match": "*"}))
    assert resp.status_code == 200
    assert len(resp.body) == 100


async def test_if_match_wildcard_on_missing_file_is_not_found(static):
    sf, _ = static
    # The precondition only runs once the file resolves; a missing path
    # still falls through to normal not-found handling (None).
    resp = await sf.handle(_req("/static/nope.bin", {"if-match": "*"}))
    assert resp is None or resp.status_code == 404


# ── If-Unmodified-Since ──────────────────────────────────────────────


async def test_if_unmodified_since_earlier_than_mtime_returns_412(static):
    sf, path = static
    older = http_date(os.stat(path).st_mtime - 3600)
    resp = await sf.handle(_req("/static/blob.bin", {"if-unmodified-since": older}))
    assert resp.status_code == 412


async def test_if_unmodified_since_not_older_returns_200(static):
    sf, path = static
    newer = http_date(os.stat(path).st_mtime + 3600)
    resp = await sf.handle(_req("/static/blob.bin", {"if-unmodified-since": newer}))
    assert resp.status_code == 200


# ── Precedence: If-Match outranks If-Unmodified-Since (§13.2.2) ───────


async def test_if_match_takes_precedence_over_if_unmodified_since(static):
    sf, path = static
    base = await sf.handle(_req("/static/blob.bin"))
    newer = http_date(os.stat(path).st_mtime + 3600)  # would satisfy IUS → 200
    resp = await sf.handle(
        _req(
            "/static/blob.bin",
            {"if-match": base.headers["ETag"], "if-unmodified-since": newer},
        )
    )
    # If-Match (concrete weak tag) wins and fails closed.
    assert resp.status_code == 412


async def test_satisfied_if_match_wildcard_suppresses_failing_ius(static):
    sf, path = static
    older = http_date(os.stat(path).st_mtime - 3600)  # would fail IUS → 412
    resp = await sf.handle(
        _req("/static/blob.bin", {"if-match": "*", "if-unmodified-since": older})
    )
    assert resp.status_code == 200


# ── Regression: read-side conditionals unchanged ─────────────────────


async def test_plain_get_still_200(static):
    sf, _ = static
    resp = await sf.handle(_req("/static/blob.bin"))
    assert resp.status_code == 200


async def test_if_none_match_still_304(static):
    sf, _ = static
    base = await sf.handle(_req("/static/blob.bin"))
    resp = await sf.handle(_req("/static/blob.bin", {"if-none-match": base.headers["ETag"]}))
    assert resp.status_code == 304
