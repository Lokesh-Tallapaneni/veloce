"""Compression pays for a thread hop only when the hop is worth paying.

The buffered path offloaded every body to the thread pool. The handoff costs a
flat ~130us regardless of size, while a body below the threshold compresses in
less than that - so a typical JSON response spent more time being scheduled
than being compressed, and the loop was freed for a shorter time than the
handoff itself occupied it.

Below the threshold the body is now compressed inline: lower latency, and the
loop held for less time than offloading would have held it anyway. Above it the
body is large enough that holding the loop would delay every other request more
than the hop does, so the hop is still paid. The streaming path already made
this trade per chunk, against the same threshold.
"""

from __future__ import annotations

import gzip

from veloce import GZipMiddleware, Response, TestClient, Veloce


def _app(**kwargs) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=100, **kwargs))

    @app.get("/body/{size:int}")
    async def body(size: int):
        return Response(body=b'{"k":"' + b"v" * size + b'"}', content_type="application/json")

    return app


def _count_offloads(monkeypatch, app: Veloce, size: int):
    """Serve `size` bytes and report how many times compression was offloaded."""
    from veloce.middleware import compression

    calls: list = []
    real = compression.offload

    def counting(fn, *args, **kwargs):
        calls.append(fn)
        return real(fn, *args, **kwargs)

    monkeypatch.setattr(compression, "offload", counting)
    with TestClient(app) as client:
        response = client.get(f"/body/{size}", headers={"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    return len(calls), response


def test_a_small_body_is_compressed_without_a_thread_hop(monkeypatch):
    count, _ = _count_offloads(monkeypatch, _app(), 2_000)
    assert count == 0


def test_a_large_body_still_goes_to_the_thread_pool(monkeypatch):
    """Past the threshold, holding the loop would delay every other request."""
    count, _ = _count_offloads(monkeypatch, _app(), 200_000)
    assert count == 1


def test_the_buffered_threshold_matches_the_streaming_one():
    middleware = GZipMiddleware()
    assert middleware.min_offload_size == middleware.min_stream_chunk_offload


def test_lowering_the_threshold_moves_the_buffered_path_too(monkeypatch):
    """A caller who tunes it gets both halves, not one."""
    count, _ = _count_offloads(monkeypatch, _app(min_stream_chunk_offload=1_000), 5_000)
    assert count == 1


def test_raising_the_threshold_keeps_a_large_body_inline(monkeypatch):
    count, _ = _count_offloads(monkeypatch, _app(min_stream_chunk_offload=10**7), 200_000)
    assert count == 0


def test_both_paths_produce_the_same_bytes():
    """Whichever branch ran, the client must get a valid gzip stream."""
    with TestClient(_app()) as client:
        small = client.get("/body/2000", headers={"Accept-Encoding": "gzip"})
        large = client.get("/body/200000", headers={"Accept-Encoding": "gzip"})
    assert gzip.decompress(small.body) == b'{"k":"' + b"v" * 2_000 + b'"}'
    assert gzip.decompress(large.body) == b'{"k":"' + b"v" * 200_000 + b'"}'


def test_a_body_below_the_minimum_size_is_still_not_compressed():
    """The inline branch must not compress what the size gate excluded."""
    with TestClient(_app()) as client:
        response = client.get("/body/10", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in {k.lower() for k in response.headers}


def test_the_compressed_length_is_still_the_advertised_one():
    with TestClient(_app()) as client:
        response = client.get("/body/2000", headers={"Accept-Encoding": "gzip"})
    assert int(response.headers["content-length"]) == len(response.body)
