"""The ETag cache is invalidated when the file changes.

`StaticFiles` caches `(etag, mtime)` per path so a hot asset is not re-hashed
per request. The entry is only usable while the recorded `mtime` still matches
the file's - otherwise a changed file would keep serving its old ETag, and a
client holding that ETag would get a `304` for content that has since changed.
Silent stale delivery, and the cache is per-process, so it persists until
restart.

**Nothing tested that.** Removing the `mtime` comparison - so any cached entry
is served regardless of the file's state - left the entire static-files suite
green. This module was written after that mutation, which is why it exists
separately from the read-path tests.
"""

from __future__ import annotations

import os

import pytest

from veloce import StaticFiles, Veloce
from veloce.testclient import TestClient


@pytest.fixture
def root(tmp_path):
    (tmp_path / "asset.txt").write_text("original")
    return tmp_path


def _client(root):
    app = Veloce(openapi_url=None)
    app.mount("/s", StaticFiles(directory=str(root)))
    return TestClient(app)


def _rewrite(path, text: str) -> None:
    """Rewrite the file and force a distinct mtime.

    Filesystem timestamps are coarse (Windows ~15 ms), so a rewrite inside the
    same tick can leave `mtime` unchanged and make the test pass for the wrong
    reason. The stamp is set explicitly instead of slept for.
    """
    path.write_text(text)
    stat = os.stat(path)
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))


# ── a changed file gets a new ETag ───────────────────────────────────


def test_a_changed_file_gets_a_new_etag(root):
    """The defect: the cached ETag was served for the new content."""
    client = _client(root)
    first = client.get("/s/asset.txt").headers["ETag"]

    _rewrite(root / "asset.txt", "changed")
    second = client.get("/s/asset.txt").headers["ETag"]

    assert second != first


def test_a_client_holding_the_old_etag_is_not_told_it_is_current(root):
    """The sharp end: a `304` for content that has changed."""
    client = _client(root)
    old = client.get("/s/asset.txt").headers["ETag"]

    _rewrite(root / "asset.txt", "changed")
    resp = client.get("/s/asset.txt", headers={"If-None-Match": old})

    assert resp.status_code == 200
    assert resp.body == b"changed"


def test_the_new_etag_does_produce_a_304(root):
    """The negative: invalidating must not break conditional requests."""
    client = _client(root)
    _rewrite(root / "asset.txt", "changed")
    current = client.get("/s/asset.txt").headers["ETag"]

    assert client.get("/s/asset.txt", headers={"If-None-Match": current}).status_code == 304


def test_the_body_follows_the_etag(root):
    client = _client(root)
    assert client.get("/s/asset.txt").body == b"original"
    _rewrite(root / "asset.txt", "changed")
    assert client.get("/s/asset.txt").body == b"changed"


# ── and an unchanged file keeps its ETag ─────────────────────────────
#
# The other direction: an invalidation that fired every time would make the
# cache pointless and break conditional requests for everyone.


def test_an_unchanged_file_keeps_its_etag(root):
    client = _client(root)
    first = client.get("/s/asset.txt").headers["ETag"]
    for _ in range(3):
        assert client.get("/s/asset.txt").headers["ETag"] == first


def test_an_unchanged_file_still_answers_304(root):
    client = _client(root)
    etag = client.get("/s/asset.txt").headers["ETag"]
    assert client.get("/s/asset.txt", headers={"If-None-Match": etag}).status_code == 304


def test_rewriting_identical_content_at_a_new_mtime_still_revalidates(root):
    """The cache keys on `mtime`, not content, so a touch invalidates. That is
    the conservative direction and is asserted so it is a decision."""
    client = _client(root)
    first = client.get("/s/asset.txt").headers["ETag"]
    _rewrite(root / "asset.txt", "original")
    assert client.get("/s/asset.txt").status_code == 200
    assert client.get("/s/asset.txt").headers["ETag"] != first


def test_two_files_do_not_share_an_entry(root):
    (root / "other.txt").write_text("other")
    client = _client(root)
    assert client.get("/s/asset.txt").headers["ETag"] != client.get("/s/other.txt").headers["ETag"]
