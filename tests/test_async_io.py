"""Tests verifying all I/O is async — no sync file reads blocking the event loop."""

import builtins
import contextlib
import io
import os
import pathlib
import time

import pytest

from tests.conftest import make_request
from veloce import Request, Response, Veloce
from veloce.contrib.staticfiles import StaticFiles
from veloce.helpers import send_from_directory_async
from veloce.http.response import _INLINE_READ_MAX, FileResponse
from veloce.middleware.compression import GZipMiddleware


class TestFileResponseAsync:
    """FileResponse.from_path() reads files in executor."""

    async def test_from_path_reads_file(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("async file content")

        resp = await FileResponse.from_path(str(test_file))
        assert resp.body == b"async file content"
        assert resp.status_code == 200

    async def test_from_path_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            await FileResponse.from_path(str(tmp_path / "nope.txt"))

    async def test_from_path_with_attachment(self, tmp_path):
        test_file = tmp_path / "report.pdf"
        test_file.write_bytes(b"%PDF-fake")

        resp = await FileResponse.from_path(str(test_file), filename="report.pdf")
        assert b"attachment" in resp.headers.get("Content-Disposition", "").encode()

    async def test_from_path_small_file_inline(self, tmp_path):
        # A file at/below the inline threshold is read on the loop (no executor
        # hop) and still returns the full body.

        test_file = tmp_path / "small.bin"
        body = b"s" * (_INLINE_READ_MAX)  # exactly the threshold -> inline
        test_file.write_bytes(body)
        resp = await FileResponse.from_path(str(test_file))
        assert resp.body == body
        assert resp.status_code == 200
        assert resp.headers.get("ETag")

    async def test_from_path_large_file_streams_the_whole_body(self, tmp_path):
        # A file above the threshold is streamed off disk in executor-read
        # chunks rather than held whole, so the bytes arrive from `_stream`
        # instead of `body`. It still advertises its length: the size is known
        # from the stat, so the response stays length-delimited.

        test_file = tmp_path / "large.bin"
        body = b"L" * (_INLINE_READ_MAX + 4096)
        test_file.write_bytes(body)
        resp = await FileResponse.from_path(str(test_file))

        assert resp.is_streamed
        assert resp.body == b""
        assert resp.headers["Content-Length"] == str(len(body))
        streamed = b"".join([chunk async for chunk in resp._stream])
        assert streamed == body

    async def test_from_path_directory_rejected(self, tmp_path):
        # A non-regular path (directory) is rejected like a missing file.
        with pytest.raises(FileNotFoundError):
            await FileResponse.from_path(str(tmp_path))


class TestSendFromDirectoryAsync:
    """Async version of send_from_directory."""

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

    async def test_static_directory_traversal_blocked(self, tmp_path):
        static_dir = tmp_path / "static"
        static_dir.mkdir()

        handler = StaticFiles(directory=str(static_dir), prefix="/static")
        resp = await handler.handle(make_request(path="/static/../../etc/passwd"))
        assert resp is not None
        assert resp.status_code == 403


class TestGZipAsync:
    """GZip compression runs in executor."""

    async def test_gzip_compresses_large_body(self):

        mw = GZipMiddleware(minimum_size=10)
        req = make_request(headers={"accept-encoding": "gzip"})
        resp = Response(body=b"x" * 1000, content_type="text/plain")

        result = await mw.process_response(req, resp)
        assert result.headers.get("Content-Encoding") == "gzip"
        assert len(result.body) < 1000

    async def test_gzip_skips_small_body(self):

        mw = GZipMiddleware(minimum_size=500)
        req = make_request(headers={"accept-encoding": "gzip"})
        resp = Response(body=b"small", content_type="text/plain")

        result = await mw.process_response(req, resp)
        assert "Content-Encoding" not in result.headers


# ── The hot path touches no filesystem ─────────────────────


def _fast_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/fast")
    async def fast(request: Request):
        return {"speed": "pure_async"}

    return app


# Every entry point that reaches the filesystem. `builtins.open` alone is not
# enough: `pathlib.Path.open` resolves `io.open` on the `io` module, so a patch
# on `builtins` never sees it.
_FILESYSTEM_ENTRY_POINTS = [(builtins, "open"), (io, "open"), (os, "open"), (os, "stat")]

# Below Python 3.11, `pathlib` reaches the filesystem through a `_NormalAccessor`
# whose `stat` / `open` are bound to the `os` and `io` functions at class
# definition, so patching the module attribute afterwards is invisible to it.
# Without these the watch still fires for a direct `open()` but not for a
# `Path.open()`, which would let the "the dispatch path touches no filesystem"
# test above pass on 3.10 while missing exactly the calls it exists to catch.
_accessor = getattr(pathlib, "_NormalAccessor", None)
if _accessor is not None:  # pragma: no cover - 3.10 only
    _FILESYSTEM_ENTRY_POINTS += [(_accessor, "stat"), (_accessor, "open")]


@contextlib.contextmanager
def _watching_the_filesystem():
    """Yield a list that records every filesystem call made inside the block."""
    calls: list[str] = []
    originals = [(module, name, getattr(module, name)) for module, name in _FILESYSTEM_ENTRY_POINTS]

    def spy(module_name: str, attr: str, original):
        def probe(*args, **kwargs):
            calls.append(f"{module_name}.{attr}")
            return original(*args, **kwargs)

        return probe

    for module, name, original in originals:
        probe = spy(getattr(module, "__name__", type(module).__name__), name, original)
        # `staticmethod` when the target is a class: the pre-3.11 pathlib
        # accessor is patched on the class and called as `self._accessor.stat(p)`,
        # so a plain function would bind and swallow the first argument as
        # `self`. The builtins it replaces do not bind, which is why the
        # unwrapped form works on every module target.
        setattr(module, name, staticmethod(probe) if isinstance(module, type) else probe)
    try:
        yield calls
    finally:
        for module, name, original in originals:
            setattr(module, name, original)


async def test_a_json_route_opens_nothing():
    """The claim the old name made, asserted directly.

    It used to time 500 dispatches against a 100us budget - a proxy for
    "something is blocking", which passes on a fast machine that *does* open a
    file and fails on a slow one that does not.

    One dispatch runs before the watch starts: the first request through a
    fresh app resolves lazy imports, and an import legitimately reads from
    disk. What the hot path must not do is read on *every* request.
    """
    app = _fast_app()
    await app.handle_request(make_request(path="/fast"))

    with _watching_the_filesystem() as calls:
        for _ in range(3):
            await app.handle_request(make_request(path="/fast"))

    assert calls == [], f"the dispatch path touched the filesystem: {calls}"


def test_the_watch_notices_a_read():
    """A watch that never fires would make the test above vacuous."""
    # The `open` is evaluated after the watch is entered, which is what makes
    # it visible to the spies.
    with (
        _watching_the_filesystem() as calls,
        pathlib.Path(__file__).open(encoding="utf-8") as handle,
    ):
        handle.readline()
    assert calls, "the filesystem watch missed a `Path.open`"


def test_the_watch_notices_a_stat():
    """The other half: existence checks are filesystem work too."""
    with _watching_the_filesystem() as calls:
        pathlib.Path(__file__).stat()
    assert calls, "the filesystem watch missed a `stat`"


def test_the_watch_restores_what_it_patched():
    """Leaving a spy installed would slow and confuse every later test."""
    before = [getattr(module, name) for module, name in _FILESYSTEM_ENTRY_POINTS]
    with _watching_the_filesystem():
        pass
    assert [getattr(module, name) for module, name in _FILESYSTEM_ENTRY_POINTS] == before


@pytest.mark.perf
async def test_a_json_route_stays_within_a_wall_clock_budget():
    """Marked `perf`: a hard-coded budget is flaky under suite CPU contention.

    Opt in with `pytest -m perf` on a quiet machine. This is a timing check and
    says so; what it used to be *named* for is the test above.
    """
    app = _fast_app()
    times = []
    for _ in range(500):
        start = time.perf_counter_ns()
        await app.handle_request(make_request(path="/fast"))
        times.append(time.perf_counter_ns() - start)

    avg_us = sum(times) / len(times) / 1000
    assert avg_us < 100, f"JSON route averaged {avg_us:.1f}us — possible sync blocking"
