"""Response.set_etag / get_etag / freeze / iter_encoded."""

from __future__ import annotations

from veloce import Response

# ── set_etag / get_etag ──────────────────────────────────────────────


def test_set_etag_quotes_unquoted_value():
    resp = Response()
    resp.set_etag("v1")
    assert resp.headers["ETag"] == '"v1"'


def test_set_etag_already_quoted_passes_through():
    resp = Response()
    resp.set_etag('"v1"')
    assert resp.headers["ETag"] == '"v1"'


def test_set_etag_weak_prefix():
    resp = Response()
    resp.set_etag("v1", weak=True)
    assert resp.headers["ETag"] == 'W/"v1"'


def test_get_etag_missing_returns_none_false():
    assert Response().get_etag() == (None, False)


def test_get_etag_strong():
    resp = Response()
    resp.set_etag("v1")
    assert resp.get_etag() == ('"v1"', False)


def test_get_etag_weak():
    resp = Response()
    resp.set_etag("v1", weak=True)
    assert resp.get_etag() == ('"v1"', True)


def test_set_etag_replaces_existing():
    resp = Response()
    resp.set_etag("v1")
    resp.set_etag("v2")
    assert resp.get_etag() == ('"v2"', False)


# ── freeze ───────────────────────────────────────────────────────────


def test_freeze_pre_computes_encode():
    resp = Response(body=b"hello")
    assert resp._encoded is None
    resp.freeze()
    assert resp._encoded is not None


def test_freeze_streaming_is_noop():
    from veloce.http.response import StreamingResponse

    async def gen():
        yield b"x"

    sr = StreamingResponse(content=gen())
    sr.freeze()
    # No encode buffer materialised — stream wasn't drained.
    assert sr._encoded is None


# ── iter_encoded ─────────────────────────────────────────────────────


def test_iter_encoded_buffered_yields_single_chunk():
    resp = Response(body=b"hello world")
    chunks = list(resp.iter_encoded())
    assert chunks == [b"hello world"]


def test_iter_encoded_empty_body():
    resp = Response()
    chunks = list(resp.iter_encoded())
    assert chunks == []


def test_iter_encoded_streaming_returns_stream():
    from veloce.http.response import StreamingResponse

    async def gen():
        yield b"x"

    sr = StreamingResponse(content=gen())
    # Iterator is the underlying async stream (proxied).
    assert sr.iter_encoded() is sr._stream
