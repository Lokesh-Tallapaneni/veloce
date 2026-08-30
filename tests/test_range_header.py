"""Request.range parsing tests."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import RangeSpec, Request, Veloce
from veloce.testclient import TestClient


def _req(range_value: str | None = None) -> Request:
    headers = {"range": range_value} if range_value else {}
    return make_request(headers=headers)


# ── RangeSpec.parse ───────────────────────────────────────────────────


def test_single_byte_range():
    r = RangeSpec.parse("bytes=0-499")
    assert r is not None
    assert r.unit == "bytes"
    assert r.ranges == [(0, 499)]


def test_multi_byte_ranges():
    r = RangeSpec.parse("bytes=0-499,1000-1499,2000-2499")
    assert r is not None
    assert r.ranges == [(0, 499), (1000, 1499), (2000, 2499)]


def test_open_ended_range():
    """`1000-` means from byte 1000 to end."""
    r = RangeSpec.parse("bytes=1000-")
    assert r is not None
    assert r.ranges == [(1000, None)]


def test_suffix_range():
    """`-500` means the last 500 bytes."""
    r = RangeSpec.parse("bytes=-500")
    assert r is not None
    assert r.ranges == [(None, 500)]


def test_mixed_ranges():
    r = RangeSpec.parse("bytes=0-99,200-,-100")
    assert r is not None
    assert r.ranges == [(0, 99), (200, None), (None, 100)]


def test_strips_whitespace_around_entries():
    r = RangeSpec.parse("bytes=  0-99 ,  200-299  ")
    assert r is not None
    assert r.ranges == [(0, 99), (200, 299)]


def test_unit_lowercased():
    r = RangeSpec.parse("BYTES=0-9")
    assert r is not None
    assert r.unit == "bytes"


# ── Malformed inputs ──────────────────────────────────────────────────


def test_empty_string_returns_none():
    assert RangeSpec.parse("") is None


def test_no_equals_returns_none():
    assert RangeSpec.parse("bytes 0-99") is None


def test_no_ranges_returns_none():
    assert RangeSpec.parse("bytes=") is None
    assert RangeSpec.parse("bytes=,,") is None


def test_invalid_integer_chunks_skipped():
    """A malformed chunk is skipped; valid chunks still parse."""
    r = RangeSpec.parse("bytes=abc-def,0-99")
    assert r is not None
    assert r.ranges == [(0, 99)]


def test_bare_dash_skipped():
    r = RangeSpec.parse("bytes=-,0-99")
    assert r is not None
    assert r.ranges == [(0, 99)]


def test_all_invalid_returns_none():
    assert RangeSpec.parse("bytes=abc-def,-,xyz") is None


# ── Request integration ──────────────────────────────────────────────


def test_request_range_missing_returns_none():
    assert _req().range is None


def test_request_range_parsed():
    r = _req("bytes=0-499").range
    assert r is not None
    assert r.unit == "bytes"
    assert r.ranges == [(0, 499)]


def test_request_range_malformed_returns_none():
    assert _req("not-a-range-value").range is None


def test_repr_includes_unit_and_ranges():
    """Cheap sanity for the debug repr."""
    r = RangeSpec.parse("bytes=0-99")
    s = repr(r)
    assert "bytes" in s
    assert "0" in s


def test_request_range_cached_identity():
    """Repeated reads of `.range` return the same object — parse runs once."""
    req = _req("bytes=0-499")
    assert req.range is req.range
    # Missing-header path: cached `None` is still cached (no re-parse).
    miss = _req()
    assert miss.range is None
    assert miss.range is miss.range


def test_range_property_cached_across_repeat_access():
    """The end-to-end counterpart of `test_request_range_cached_identity`.

    Caching also holds on a `Request` the ASGI path built, not only on
    one this module builds by hand.
    """
    app = Veloce(openapi_url=None)
    observed = {}

    @app.get("/range")
    async def range_route(request: Request):
        r1 = request.range
        r2 = request.range
        observed["same"] = r1 is r2
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/range", headers={"Range": "bytes=0-100"})
    assert observed["same"] is True
