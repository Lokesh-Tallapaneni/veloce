"""StaticFiles html-mode directory redirect and 404.html fallback."""

from __future__ import annotations

import pytest

from veloce import Request
from veloce.contrib.staticfiles import StaticFiles
from veloce.status import (
    HTTP_307_TEMPORARY_REDIRECT,
    HTTP_308_PERMANENT_REDIRECT,
)


def _req(path: str, query_string: str = "") -> Request:
    return Request(method="GET", path=path, query_string=query_string, headers={}, body=b"")


# ── Directory trailing-slash redirect (html mode) ──


@pytest.mark.asyncio
async def test_subdir_index_slashless_redirects(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.html").write_text("DOC INDEX")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", html=True)
    resp = await sf.handle(_req("/s/docs"))
    assert resp is not None
    assert resp.status_code == HTTP_307_TEMPORARY_REDIRECT
    assert resp.headers["Location"] == "/s/docs/"


@pytest.mark.asyncio
async def test_subdir_index_with_slash_serves_index(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.html").write_text("DOC INDEX")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", html=True)
    resp = await sf.handle(_req("/s/docs/"))
    assert resp is not None
    assert resp.status_code == 200
    assert resp.body == b"DOC INDEX"
    assert resp.content_type.startswith("text/html")


@pytest.mark.asyncio
async def test_redirect_preserves_query_string(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.html").write_text("X")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", html=True)
    resp = await sf.handle(_req("/s/docs", query_string="v=2&lang=en"))
    assert resp.status_code == HTTP_307_TEMPORARY_REDIRECT
    assert resp.headers["Location"] == "/s/docs/?v=2&lang=en"


@pytest.mark.asyncio
async def test_redirect_status_308(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.html").write_text("X")
    sf = StaticFiles(
        directory=str(tmp_path),
        prefix="/s",
        html=True,
        redirect_status=HTTP_308_PERMANENT_REDIRECT,
    )
    resp = await sf.handle(_req("/s/docs"))
    assert resp.status_code == HTTP_308_PERMANENT_REDIRECT


@pytest.mark.asyncio
async def test_invalid_redirect_status_rejected(tmp_path):
    with pytest.raises(ValueError, match="redirect_status must be 307 or 308"):
        StaticFiles(directory=str(tmp_path), prefix="/s", html=True, redirect_status=301)


@pytest.mark.asyncio
async def test_subdir_no_index_without_html_is_miss(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.html").write_text("X")
    # html=False: directory mapping is not consulted, no redirect, no index.
    sf = StaticFiles(directory=str(tmp_path), prefix="/s")
    assert await sf.handle(_req("/s/docs")) is None
    assert await sf.handle(_req("/s/docs/")) is None


@pytest.mark.asyncio
async def test_subdir_without_index_no_redirect(tmp_path):
    # html=True but no index.html in the subdir → no redirect, falls to 404 path.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("x")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", html=True)
    assert await sf.handle(_req("/s/docs")) is None


@pytest.mark.asyncio
async def test_directory_index_listing_supersedes_when_no_index_html(tmp_path):
    # html + directory_index, subdir without index.html → listing of the subdir.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("x")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", html=True, directory_index=True)
    resp = await sf.handle(_req("/s/docs/"))
    assert resp.status_code == 200
    assert "a.txt" in resp.body.decode()


@pytest.mark.asyncio
async def test_index_html_wins_over_directory_listing(tmp_path):
    # When both index.html exists and directory_index is on, the index file is
    # served (after the slash redirect), not a generated listing.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.html").write_text("HELLO")
    (docs / "a.txt").write_text("x")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", html=True, directory_index=True)
    resp = await sf.handle(_req("/s/docs/"))
    assert resp.body == b"HELLO"


# ── 404.html custom not-found page (html mode) ──


@pytest.mark.asyncio
async def test_custom_404_html_served(tmp_path):
    (tmp_path / "404.html").write_text("<h1>Gone</h1>")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", html=True)
    resp = await sf.handle(_req("/s/missing.txt"))
    assert resp is not None
    assert resp.status_code == 404
    assert resp.body == b"<h1>Gone</h1>"
    assert resp.content_type.startswith("text/html")


@pytest.mark.asyncio
async def test_no_404_html_returns_none(tmp_path):
    # html=True but no 404.html present → fall through to dispatch 404 (None).
    (tmp_path / "a.txt").write_text("x")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", html=True)
    assert await sf.handle(_req("/s/missing.txt")) is None


@pytest.mark.asyncio
async def test_404_html_not_used_without_html_mode(tmp_path):
    (tmp_path / "404.html").write_text("<h1>Gone</h1>")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s")
    assert await sf.handle(_req("/s/missing.txt")) is None


@pytest.mark.asyncio
async def test_404_html_directly_requestable(tmp_path):
    # The 404.html file is still a normal asset when requested by name.
    (tmp_path / "404.html").write_text("<h1>Gone</h1>")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", html=True)
    resp = await sf.handle(_req("/s/404.html"))
    assert resp.status_code == 200
    assert resp.body == b"<h1>Gone</h1>"


@pytest.mark.asyncio
async def test_existing_file_unaffected_by_html_dir_logic(tmp_path):
    (tmp_path / "app.js").write_text("console.log(1)")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", html=True)
    resp = await sf.handle(_req("/s/app.js"))
    assert resp.status_code == 200
    assert resp.body == b"console.log(1)"
