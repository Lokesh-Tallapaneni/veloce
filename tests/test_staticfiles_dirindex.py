"""StaticFiles directory listing."""

from __future__ import annotations

import pytest

from veloce import Request
from veloce.contrib.staticfiles import StaticFiles


def _req(path: str) -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


@pytest.mark.asyncio
async def test_directory_index_off_by_default(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"a")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s")
    # Hitting a directory (no index.html) returns None → 404.
    resp = await sf.handle(_req("/s/"))
    assert resp is None


@pytest.mark.asyncio
async def test_directory_index_lists_files(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", directory_index=True)
    resp = await sf.handle(_req("/s/"))
    assert resp is not None
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "a.txt" in body
    assert "b.txt" in body
    assert resp.content_type.startswith("text/html")


@pytest.mark.asyncio
async def test_directory_index_hides_dotfiles(tmp_path):
    (tmp_path / "visible.txt").write_bytes(b"x")
    (tmp_path / ".hidden").write_bytes(b"x")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", directory_index=True)
    resp = await sf.handle(_req("/s/"))
    body = resp.body.decode()
    assert "visible.txt" in body
    assert ".hidden" not in body


@pytest.mark.asyncio
async def test_directory_index_marks_subdirectories(tmp_path):
    (tmp_path / "sub").mkdir()
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", directory_index=True)
    resp = await sf.handle(_req("/s/"))
    body = resp.body.decode()
    # Subdir gets a trailing slash in the rendered link.
    assert 'href="sub/">sub/' in body


@pytest.mark.asyncio
async def test_directory_index_escapes_dangerous_filenames(tmp_path):
    # `<` / `>` are not allowed in Windows filenames, but `&` is. The
    # render path uses `html.escape` which escapes `&` → `&amp;`,
    # `<` → `&lt;`, etc. We use `&` to exercise the same code path
    # cross-platform.
    (tmp_path / "a&b.txt").write_text("x")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", directory_index=True)
    resp = await sf.handle(_req("/s/"))
    body = resp.body.decode()
    # Filename appears HTML-escaped — raw `&` does not.
    assert "a&amp;b.txt" in body


@pytest.mark.asyncio
async def test_directory_index_symlinked_dir_not_marked_as_dir(tmp_path):
    """Symlinks in the listing are classified by the symlink itself.

    `os.scandir`'s `is_dir(follow_symlinks=False)` deliberately does not
    follow the link target — a symlink to a real directory renders as a
    plain entry rather than a directory entry. Matches the symlink-safety
    stance the static handler already takes elsewhere (refusing to serve
    a file whose realpath escapes the served root).
    """
    import os

    (tmp_path / "real").mkdir()
    try:
        os.symlink(str(tmp_path / "real"), str(tmp_path / "link"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform / user")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", directory_index=True)
    resp = await sf.handle(_req("/s/"))
    body = resp.body.decode()
    # Real dir renders with trailing slash; the symlink to it does not.
    assert 'href="real/">real/' in body
    assert 'href="link">link<' in body


@pytest.mark.asyncio
async def test_directory_index_hides_external_symlink(tmp_path):
    """Symlinks whose target escapes the served root are dropped from the listing."""
    import os

    served = tmp_path / "served"
    served.mkdir()
    (served / "in_root.txt").write_text("ok")
    # Out-of-root targets, parallel to (not under) the served directory.
    (tmp_path / "secret.txt").write_text("secret")
    (tmp_path / "secret_dir").mkdir()
    try:
        os.symlink(str(tmp_path / "secret.txt"), str(served / "esc_file"))
        os.symlink(str(tmp_path / "secret_dir"), str(served / "esc_dir"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform / user")
    sf = StaticFiles(directory=str(served), prefix="/s", directory_index=True)
    resp = await sf.handle(_req("/s/"))
    body = resp.body.decode()
    assert "in_root.txt" in body
    assert "esc_file" not in body
    assert "esc_dir" not in body


@pytest.mark.asyncio
async def test_directory_index_keeps_internal_symlink(tmp_path):
    """A symlink pointing to another entry UNDER the served root stays listed."""
    import os

    served = tmp_path / "served"
    served.mkdir()
    (served / "real").mkdir()
    try:
        os.symlink(str(served / "real"), str(served / "internal_link"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform / user")
    sf = StaticFiles(directory=str(served), prefix="/s", directory_index=True)
    resp = await sf.handle(_req("/s/"))
    body = resp.body.decode()
    assert "internal_link" in body


@pytest.mark.asyncio
async def test_directory_index_does_not_supersede_index_html(tmp_path):
    """If `html=True` and `index.html` exists, the file is served, not a listing."""
    (tmp_path / "index.html").write_text("HELLO")
    (tmp_path / "other.txt").write_text("x")
    sf = StaticFiles(directory=str(tmp_path), prefix="/s", html=True, directory_index=True)
    resp = await sf.handle(_req("/s/"))
    assert resp.body == b"HELLO"
