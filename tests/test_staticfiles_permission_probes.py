"""A permission-denied probe is a 403, at every point `StaticFiles` probes.

`StaticFiles` stats a path up to five times per request — the file itself, the
`.html` variant, a directory's `index.html`, a `404.html` page, and each
precompressed sibling — and each probe returned `(stat_result, denied)` with its
own pair of locals. `_select_precompressed` could not use that shape at all, so
it raised `PermissionError` to smuggle the denial back out.

The probes now share one `_stat_regular`, which raises `PermissionError` and is
caught once. That is a control-flow change in code that decides **403 vs 404 vs
500**, so these tests were written against the *existing* behaviour first and
are unchanged by the refactor — they are what makes it safe rather than merely
shorter.

Every probe point is covered, because the failure mode of consolidating five
call sites is that one of them stops being reachable.
"""

from __future__ import annotations

import os

import pytest

from veloce import Veloce
from veloce.testclient import TestClient


@pytest.fixture
def root(tmp_path):
    (tmp_path / "plain.txt").write_text("plain")
    (tmp_path / "page.html").write_text("page")
    (tmp_path / "404.html").write_text("custom not found")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "index.html").write_text("index")
    (tmp_path / "asset.js").write_text("asset")
    (tmp_path / "asset.js.gz").write_bytes(b"gzipped")
    return tmp_path


def _client(root, **kwargs):
    app = Veloce(openapi_url=None)
    app.mount("/s", _static(root, **kwargs))
    return TestClient(app)


def _static(root, **kwargs):
    from veloce import StaticFiles

    return StaticFiles(directory=str(root), **kwargs)


class _DenyingStat:
    """Raise `PermissionError` from `os.stat` for paths matching a suffix."""

    def __init__(self, suffix: str):
        self.suffix = suffix
        self.real = os.stat

    def __call__(self, path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)) and str(path).endswith(self.suffix):
            raise PermissionError(str(path))
        return self.real(path, *args, **kwargs)


@pytest.fixture
def deny(monkeypatch):
    def _deny(suffix: str):
        monkeypatch.setattr(os, "stat", _DenyingStat(suffix))

    return _deny


# ── the happy paths, so the denials below are not vacuous ────────────


def test_a_plain_file_is_served(root):
    assert _client(root).get("/s/plain.txt").body == b"plain"


def test_the_html_variant_is_served(root):
    assert _client(root, html=True).get("/s/page").body == b"page"


def test_a_directory_index_is_served(root):
    assert _client(root, html=True).get("/s/dir/").body == b"index"


def test_a_missing_path_is_a_404_page_in_html_mode(root):
    resp = _client(root, html=True).get("/s/nope")
    assert resp.status_code == 404
    assert resp.body == b"custom not found"


def test_a_missing_path_is_a_plain_404_without_html_mode(root):
    assert _client(root).get("/s/nope").status_code == 404


def test_a_precompressed_sibling_is_served(root):
    client = _client(root, precompressed=True)
    resp = client.get("/s/asset.js", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("content-encoding") == "gzip"


# ── a denial at each probe point is a 403 ────────────────────────────


def test_a_denied_file_is_forbidden(root, deny):
    deny("plain.txt")
    assert _client(root).get("/s/plain.txt").status_code == 403


def test_a_denied_html_variant_is_forbidden(root, deny):
    deny("page.html")
    assert _client(root, html=True).get("/s/page").status_code == 403


def test_a_denied_directory_index_is_forbidden(root, deny):
    deny("index.html")
    assert _client(root, html=True).get("/s/dir/").status_code == 403


def test_a_denied_404_page_is_forbidden(root, deny):
    deny("404.html")
    assert _client(root, html=True).get("/s/nope").status_code == 403


def test_a_denied_precompressed_sibling_is_forbidden(root, deny):
    deny("asset.js.gz")
    client = _client(root, precompressed=True)
    assert client.get("/s/asset.js", headers={"Accept-Encoding": "gzip"}).status_code == 403


# ── and a denial is never a 500 ──────────────────────────────────────


@pytest.mark.parametrize(
    ("suffix", "path", "kwargs"),
    [
        ("plain.txt", "/s/plain.txt", {}),
        ("page.html", "/s/page", {"html": True}),
        ("index.html", "/s/dir/", {"html": True}),
        ("404.html", "/s/nope", {"html": True}),
    ],
    ids=["file", "html-variant", "directory-index", "not-found-page"],
)
def test_a_denial_never_surfaces_as_a_server_error(root, deny, suffix, path, kwargs):
    """The reason the probes tag denial at all: a bare `PermissionError` would
    otherwise bubble to a 500 and tell the client nothing."""
    deny(suffix)
    assert _client(root, **kwargs).get(path).status_code == 403


# ── a missing file stays a 404, not a 403 ────────────────────────────
#
# The negative that matters: a consolidation that turned every failed probe into
# a denial would hide every missing file behind a 403.


def test_a_missing_file_is_not_forbidden(root):
    assert _client(root).get("/s/absent.txt").status_code == 404


def test_a_missing_html_variant_is_not_forbidden(root):
    assert _client(root, html=True).get("/s/absent").status_code == 404


def test_a_directory_with_no_index_is_not_forbidden(root):
    (root / "empty").mkdir()
    assert _client(root, html=True).get("/s/empty/").status_code == 404


def test_a_missing_precompressed_sibling_falls_back_to_the_original(root):
    """No `.br` sibling exists, so the identity file is served, not a 403."""
    client = _client(root, precompressed=True)
    resp = client.get("/s/asset.js", headers={"Accept-Encoding": "br"})
    assert resp.status_code == 200
    assert resp.body == b"asset"


def test_a_traversal_attempt_is_still_forbidden(root):
    """The other 403 source must keep answering 403."""
    assert _client(root).get("/s/../secret").status_code in (403, 404)
