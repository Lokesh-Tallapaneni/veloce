"""Tests verifying all I/O is async — no sync file reads blocking the event loop."""

import pytest

from tests.conftest import make_request
from veloce import Request, Response, Veloce
from veloce.contrib.staticfiles import StaticFiles
from veloce.helpers import send_from_directory_async
from veloce.http.response import FileResponse


class TestFileResponseAsync:
    """FileResponse.from_path() reads files in executor."""

    @pytest.mark.asyncio
    async def test_from_path_reads_file(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("async file content")

        resp = await FileResponse.from_path(str(test_file))
        assert resp.body == b"async file content"
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_from_path_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            await FileResponse.from_path(str(tmp_path / "nope.txt"))

    @pytest.mark.asyncio
    async def test_from_path_with_attachment(self, tmp_path):
        test_file = tmp_path / "report.pdf"
        test_file.write_bytes(b"%PDF-fake")

        resp = await FileResponse.from_path(str(test_file), filename="report.pdf")
        assert b"attachment" in resp.headers.get("Content-Disposition", "").encode()


class TestSendFromDirectoryAsync:
    """Async version of send_from_directory."""

    @pytest.mark.asyncio
    async def test_send_file_async(self, tmp_path):
        test_file = tmp_path / "data.csv"
        # Write bytes directly: `write_text` would translate `\n` to the
        # platform newline (`\r\n` on Windows), but the handler returns
        # the file's exact bytes — so the fixture must be byte-exact.
        test_file.write_bytes(b"a,b,c\n1,2,3")

        resp = await send_from_directory_async(str(tmp_path), "data.csv")
        assert resp.body == b"a,b,c\n1,2,3"


class TestStaticFilesAsync:
    """StaticFiles handler uses executor for all file I/O."""

    @pytest.mark.asyncio
    async def test_static_file_served_async(self, tmp_path):
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "style.css").write_text("body { color: red; }")

        handler = StaticFiles(directory=str(static_dir), prefix="/static")
        req = make_request(path="/static/style.css")
        resp = await handler.handle(req)

        assert resp is not None
        assert resp.status_code == 200
        assert b"body { color: red; }" in resp.body

    @pytest.mark.asyncio
    async def test_static_etag_caching(self, tmp_path):
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "app.js").write_text("console.log('hi')")

        handler = StaticFiles(directory=str(static_dir), prefix="/static")

        # First request
        resp1 = await handler.handle(make_request(path="/static/app.js"))
        assert resp1.status_code == 200
        etag = resp1.headers["ETag"]

        # Second request with matching ETag
        resp2 = await handler.handle(
            make_request(path="/static/app.js", headers={"if-none-match": etag})
        )
        assert resp2.status_code == 304

    @pytest.mark.asyncio
    async def test_static_directory_traversal_blocked(self, tmp_path):
        static_dir = tmp_path / "static"
        static_dir.mkdir()

        handler = StaticFiles(directory=str(static_dir), prefix="/static")
        resp = await handler.handle(make_request(path="/static/../../etc/passwd"))
        assert resp is not None
        assert resp.status_code == 403


class TestGZipAsync:
    """GZip compression runs in executor."""

    @pytest.mark.asyncio
    async def test_gzip_compresses_large_body(self):
        from veloce.middleware.compression import GZipMiddleware

        mw = GZipMiddleware(minimum_size=10)
        req = make_request(headers={"accept-encoding": "gzip"})
        resp = Response(body=b"x" * 1000, content_type="text/plain")

        result = await mw.process_response(req, resp)
        assert result.headers.get("Content-Encoding") == "gzip"
        assert len(result.body) < 1000

    @pytest.mark.asyncio
    async def test_gzip_skips_small_body(self):
        from veloce.middleware.compression import GZipMiddleware

        mw = GZipMiddleware(minimum_size=500)
        req = make_request(headers={"accept-encoding": "gzip"})
        resp = Response(body=b"small", content_type="text/plain")

        result = await mw.process_response(req, resp)
        assert "Content-Encoding" not in result.headers


@pytest.mark.perf
class TestNoSyncIOInHotPath:
    """Verify the hot path (simple JSON route) has no sync I/O calls.

    Marked `perf`: the lone test in this class asserts a hard-coded
    wall-clock budget, which is flaky under full-suite CPU contention.
    Opt in with `pytest -m perf` on a quiet machine.
    """

    @pytest.mark.asyncio
    async def test_json_route_is_pure_async(self):
        """A simple JSON route should never touch the filesystem or block."""
        app = Veloce(openapi_url=None)

        @app.get("/fast")
        async def fast(request: Request):
            return {"speed": "pure_async"}

        # If this takes > 1ms on avg, something is blocking
        import time

        times = []
        for _ in range(500):
            start = time.perf_counter_ns()
            await app.handle_request(make_request(path="/fast"))
            times.append(time.perf_counter_ns() - start)

        avg_us = sum(times) / len(times) / 1000
        assert avg_us < 100, f"JSON route averaged {avg_us:.1f}us — possible sync blocking"
