"""StaticFiles ETag cache — bounded LRU eviction."""

from __future__ import annotations

import asyncio
import os

from veloce import Request
from veloce.contrib.staticfiles import StaticFiles


def test_staticfiles_etag_cache_is_bounded(tmp_path):
    """The ETag cache must not grow without limit. Hammer it with more
    distinct files than the cap allows and assert the cap is honoured;
    least-recently-used entries are evicted."""

    async def hammer() -> int:
        sf = StaticFiles(str(tmp_path), prefix="/static")
        # Tighten the cap so the test stays small/fast.
        sf.ETAG_CACHE_MAX = 4
        for i in range(20):
            p = tmp_path / f"f{i}.txt"
            p.write_text(f"file-{i}")
            req = Request(
                method="GET",
                path=f"/static/f{i}.txt",
                query_string="",
                headers={},
                body=b"",
            )
            resp = await sf.handle(req)
            assert resp is not None
            assert resp.status_code == 200
        return len(sf._etag_cache)

    size = asyncio.run(hammer())
    assert size == 4  # capped, not 20


def test_staticfiles_etag_lru_evicts_oldest(tmp_path):
    """Touching an old entry should refresh its position so a later
    eviction drops the truly oldest one."""

    async def go() -> dict:
        sf = StaticFiles(str(tmp_path), prefix="/static")
        sf.ETAG_CACHE_MAX = 3
        for i in range(3):
            (tmp_path / f"f{i}.txt").write_text(f"f{i}")

        async def hit(name: str) -> None:
            req = Request(
                method="GET",
                path=f"/static/{name}",
                query_string="",
                headers={},
                body=b"",
            )
            await sf.handle(req)

        await hit("f0.txt")
        await hit("f1.txt")
        await hit("f2.txt")
        # Refresh f0 so it is now the most-recently-used; f1 becomes
        # the oldest.
        await hit("f0.txt")
        # Adding a fourth entry should evict f1, not f0.
        (tmp_path / "f3.txt").write_text("f3")
        await hit("f3.txt")
        keys = {os.path.basename(k) for k in sf._etag_cache}
        return keys

    keys = asyncio.run(go())
    assert "f1.txt" not in keys
    assert {"f0.txt", "f2.txt", "f3.txt"} <= keys
