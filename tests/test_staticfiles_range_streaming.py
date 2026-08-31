"""A large byte range streams instead of being read whole into memory.

`StaticFiles` streams a file at or above `STREAM_THRESHOLD` so a large download
does not add its own size to the worker's RSS. The range branch returned before
that check, on the reasoning recorded in the comment beside it - "a range is
already bounded by the client".

It is bounded by the client only if the client chooses to bound it.
`Range: bytes=0-` is a well-formed range covering the entire file, so a 500 MiB
asset requested that way was read whole into memory, `STREAM_THRESHOLD` and all;
ten such requests are ~5 GB of RSS from ten cheap, valid GETs.

The threshold now applies to the resolved slice rather than to whether a range
was asked for at all. A small slice of a huge file still buffers - that was
always the right call, and it is the case these tests pin hardest, because the
obvious fix (stream whenever a range is requested) would make every small range
pay chunked transfer.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.staticfiles import StaticFiles
from veloce.testclient import TestClient

#: Small enough to keep the suite fast, large enough to sit either side of it.
_THRESHOLD = 64 * 1024
_SIZE = _THRESHOLD * 4


@pytest.fixture
def client(tmp_path):
    (tmp_path / "big.bin").write_bytes(bytes(range(256)) * (_SIZE // 256))
    (tmp_path / "small.bin").write_bytes(b"s" * 512)

    class Bounded(StaticFiles):
        STREAM_THRESHOLD = _THRESHOLD

    app = Veloce(openapi_url=None)
    app.mount("/static", Bounded(directory=str(tmp_path)))
    return TestClient(app)


def _get(client, path, headers=None):
    return client.get(path, headers=headers or {})


def _is_streamed(resp) -> bool:
    """A streamed response carries no `Content-Length` - see the branch's note."""
    return "content-length" not in {k.lower() for k in resp.headers}


# ── the open-ended range, which is the whole file ────────────────────


# The defect is asserted through the response shape rather than by watching RSS,
# which is not measurable reliably in-process. `_is_streamed` is the observable
# the buffered branch cannot produce.


def test_an_open_ended_range_is_a_206_over_the_whole_file(client):
    resp = _get(client, "/static/big.bin", {"Range": "bytes=0-"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 0-{_SIZE - 1}/{_SIZE}"


def test_an_open_ended_range_over_a_large_file_is_streamed(client):
    resp = _get(client, "/static/big.bin", {"Range": "bytes=0-"})
    assert _is_streamed(resp)


def test_an_open_ended_range_still_delivers_every_byte(client):
    """Streaming must not truncate: the bytes are the contract, not the shape."""
    resp = _get(client, "/static/big.bin", {"Range": "bytes=0-"})
    assert len(resp.body) == _SIZE
    assert resp.body == (bytes(range(256)) * (_SIZE // 256))


def test_a_large_explicit_range_also_streams(client):
    """`bytes=0-<huge>` is the same problem spelled out."""
    resp = _get(client, "/static/big.bin", {"Range": f"bytes=0-{_SIZE - 1}"})
    assert resp.status_code == 206
    assert _is_streamed(resp)
    assert len(resp.body) == _SIZE


def test_a_large_suffix_range_also_streams(client):
    """`bytes=-<huge>` reaches the same resolution by the other branch."""
    resp = _get(client, "/static/big.bin", {"Range": f"bytes=-{_SIZE}"})
    assert resp.status_code == 206
    assert _is_streamed(resp)
    assert len(resp.body) == _SIZE


def test_a_mid_file_large_range_streams_the_right_bytes(client):
    start = _SIZE // 4
    resp = _get(client, "/static/big.bin", {"Range": f"bytes={start}-"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes {start}-{_SIZE - 1}/{_SIZE}"
    assert resp.body == (bytes(range(256)) * (_SIZE // 256))[start:]


# ── the small range must NOT start streaming ─────────────────────────
#
# The negatives, and the reason the threshold is applied to the resolved slice
# rather than to the presence of a `Range` header: a 100-byte slice of a 10 GiB
# file should stay one buffered message.


def test_a_small_range_over_a_large_file_stays_buffered(client):
    resp = _get(client, "/static/big.bin", {"Range": "bytes=0-99"})
    assert resp.status_code == 206
    assert resp.headers["content-length"] == "100"
    assert len(resp.body) == 100


def test_a_small_range_over_a_small_file_stays_buffered(client):
    resp = _get(client, "/static/small.bin", {"Range": "bytes=0-9"})
    assert resp.status_code == 206
    assert resp.headers["content-length"] == "10"
    assert resp.body == b"ssssssssss"


def test_an_open_ended_range_over_a_small_file_stays_buffered(client):
    """`bytes=0-` is only a problem when the file is large."""
    resp = _get(client, "/static/small.bin", {"Range": "bytes=0-"})
    assert resp.status_code == 206
    assert resp.headers["content-length"] == "512"


def test_a_range_exactly_below_the_threshold_stays_buffered(client):
    resp = _get(client, "/static/big.bin", {"Range": f"bytes=0-{_THRESHOLD - 2}"})
    assert resp.headers["content-length"] == str(_THRESHOLD - 1)


def test_a_range_exactly_at_the_threshold_streams(client):
    resp = _get(client, "/static/big.bin", {"Range": f"bytes=0-{_THRESHOLD - 1}"})
    assert _is_streamed(resp)
    assert len(resp.body) == _THRESHOLD


# ── every other header the range branch sets is unchanged ────────────


@pytest.mark.parametrize("rng", ["bytes=0-", "bytes=0-99"])
def test_the_range_response_keeps_its_validators(client, rng):
    resp = _get(client, "/static/big.bin", {"Range": rng})
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["etag"]
    assert resp.headers["last-modified"]
    assert resp.headers["cache-control"]


@pytest.mark.parametrize("rng", ["bytes=0-", "bytes=0-99"])
def test_the_range_response_keeps_its_content_range(client, rng):
    resp = _get(client, "/static/big.bin", {"Range": rng})
    assert resp.headers["content-range"].startswith("bytes 0-")
    assert resp.headers["content-range"].endswith(f"/{_SIZE}")


@pytest.mark.parametrize("rng", ["bytes=0-", "bytes=0-99"])
def test_the_range_response_keeps_its_content_type(client, rng):
    assert _get(client, "/static/big.bin", {"Range": rng}).headers["content-type"]


# ── the refusals are untouched ───────────────────────────────────────


def test_a_range_past_the_end_is_still_416(client):
    resp = _get(client, "/static/small.bin", {"Range": "bytes=9999-"})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == "bytes */512"


def test_an_inverted_range_is_still_416(client):
    assert _get(client, "/static/small.bin", {"Range": "bytes=100-50"}).status_code == 416


def test_a_multi_range_request_falls_back_to_the_whole_file(client):
    """Multiple ranges are not served as multipart; the full body is correct."""
    resp = _get(client, "/static/small.bin", {"Range": "bytes=0-9,20-29"})
    assert resp.status_code == 200
    assert len(resp.body) == 512


def test_a_request_with_no_range_is_unaffected(client):
    resp = _get(client, "/static/big.bin")
    assert resp.status_code == 200
    assert _is_streamed(resp)
    assert len(resp.body) == _SIZE


def test_a_small_file_with_no_range_is_still_buffered(client):
    resp = _get(client, "/static/small.bin")
    assert resp.status_code == 200
    assert resp.headers["content-length"] == "512"
