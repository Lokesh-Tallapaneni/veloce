"""HTTP date helpers — `http_date` / `parse_date` (RFC 9110 Sec. 5.6.7).

RFC 9110 mandates the IMF-fixdate form for HTTP date headers
(`Date`, `Last-Modified`, `Expires`, `If-Modified-Since`, ...):

    Sun, 06 Nov 1994 08:49:37 GMT

`http_date` renders that form from a variety of inputs; `parse_date`
accepts the IMF-fixdate form (and the two obsolete forms RFC 9110
still requires recipients to tolerate) and returns a timezone-aware
`datetime` in UTC.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from time import struct_time, time

# `http_date(None)` is the per-response `Date:` header. Formatting that
# string costs ~3 us on every response but only changes once a second;
# cache the result keyed by the whole-second bucket.
_now_cache: tuple[int, str] = (-1, "")


def _http_date_now() -> str:
    """Return the current HTTP-date, cached to a 1-second granularity."""
    global _now_cache
    sec = int(time())
    bucket, cached = _now_cache
    if sec != bucket:
        cached = formatdate(sec, usegmt=True)
        _now_cache = (sec, cached)
    return cached


def http_date(value: datetime | date | struct_time | int | float | None = None) -> str:
    """Render `value` as an RFC 9110 IMF-fixdate string.

    Accepts:
    - `None` -> current time.
    - `datetime` -> naive datetimes are assumed UTC.
    - `date` -> midnight UTC of that day.
    - numeric -> POSIX timestamp (seconds since the epoch).
    - `struct_time` -> as returned by `time.gmtime()`.

    Always emits the `GMT` zone suffix per the spec.
    """
    if value is None:
        return _http_date_now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        ts = value.timestamp()
    elif isinstance(value, struct_time):
        ts = calendar.timegm(value)
    elif isinstance(value, date):
        # `date` (not datetime) -> midnight UTC.
        ts = datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp()
    else:
        ts = float(value)
    return formatdate(ts, usegmt=True)


def parse_date(value: str | None) -> datetime | None:
    """Parse an HTTP date header into a timezone-aware UTC `datetime`.

    Returns `None` for empty input or any value that doesn't parse as
    a recognised HTTP date. The result is always tz-aware (UTC) so it
    can be compared directly with `datetime.now(timezone.utc)`.
    """
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
