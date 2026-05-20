"""http_date / parse_date — RFC 9110 §5.6.7 date helpers."""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

from veloce.http.dates import http_date, parse_date

# ── http_date ───────────────────────────────────────────────────────


def test_http_date_from_timestamp():
    # 784111777 = Sun, 06 Nov 1994 08:49:37 GMT
    assert http_date(784111777) == "Sun, 06 Nov 1994 08:49:37 GMT"


def test_http_date_from_aware_datetime():
    dt = datetime(1994, 11, 6, 8, 49, 37, tzinfo=timezone.utc)
    assert http_date(dt) == "Sun, 06 Nov 1994 08:49:37 GMT"


def test_http_date_from_naive_datetime_assumes_utc():
    dt = datetime(1994, 11, 6, 8, 49, 37)
    assert http_date(dt) == "Sun, 06 Nov 1994 08:49:37 GMT"


def test_http_date_from_date_is_midnight_utc():
    assert http_date(date(1994, 11, 6)) == "Sun, 06 Nov 1994 00:00:00 GMT"


def test_http_date_from_struct_time():
    st = time.gmtime(784111777)
    assert http_date(st) == "Sun, 06 Nov 1994 08:49:37 GMT"


def test_http_date_none_is_now():
    out = http_date()
    # Ends with GMT and parses back to roughly now.
    assert out.endswith("GMT")
    parsed = parse_date(out)
    assert parsed is not None
    delta = abs((datetime.now(timezone.utc) - parsed).total_seconds())
    assert delta < 5


def test_http_date_always_emits_gmt():
    assert http_date(0).endswith("GMT")


# ── parse_date ──────────────────────────────────────────────────────


def test_parse_date_imf_fixdate():
    dt = parse_date("Sun, 06 Nov 1994 08:49:37 GMT")
    assert dt == datetime(1994, 11, 6, 8, 49, 37, tzinfo=timezone.utc)


def test_parse_date_returns_aware_utc():
    dt = parse_date("Sun, 06 Nov 1994 08:49:37 GMT")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_parse_date_none_for_empty():
    assert parse_date("") is None
    assert parse_date(None) is None


def test_parse_date_none_for_garbage():
    assert parse_date("not a date") is None


def test_parse_date_roundtrips_with_http_date():
    original = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    rendered = http_date(original)
    assert parse_date(rendered) == original


def test_parse_date_handles_non_gmt_offset():
    # +0000 explicit offset normalises to UTC.
    dt = parse_date("Sun, 06 Nov 1994 08:49:37 +0000")
    assert dt == datetime(1994, 11, 6, 8, 49, 37, tzinfo=timezone.utc)
