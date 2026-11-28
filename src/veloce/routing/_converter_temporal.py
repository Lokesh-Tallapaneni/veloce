"""Temporal and decimal segment converters — date, datetime, time, timedelta, decimal.

Split from the other built-in converters because the datetime parse selects a C
ISO-8601 implementation at import time; `_parse_datetime` is looked up in this
module, so a test that swaps it patches it here.
"""

from __future__ import annotations

import datetime
import decimal
import re
from typing import Any, cast

from veloce.routing._converter_base import Converter

# A C ISO-8601 parser `_parse_datetime` selects over the stdlib when installed.
try:  # pragma: no cover - one branch per environment
    from ciso8601 import parse_datetime as _parse_datetime_ciso

    _HAS_CISO8601 = True
except ImportError:  # pragma: no cover
    _parse_datetime_ciso = cast("Any", None)
    _HAS_CISO8601 = False


# Anchored prefilters keep the try/except parse off the hot path for a reject.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
# The offset is captured because the parse below needs to know it is there.
_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:([+-][\d:]+)|Z)?$")
_TIME_RE = re.compile(r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[+-][\d:]+|Z)?$")
# ISO 8601 duration: at least one component required.
_TIMEDELTA_RE = re.compile(
    r"P(?=\d|T)(?:(\d+)D)?(?:T(?=\d)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)
# Python's `str(timedelta)`, so a real one round-trips through `url_for`.
_TIMEDELTA_STR_RE = re.compile(
    r"(?:(?P<days>-?\d+) days?, )?(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2}(?:\.\d{1,6})?)$"
)
_DECIMAL_RE = re.compile(r"[+-]?\d+(?:\.\d+)?$")
_MAX_DECIMAL_CHARS = 40


def _normalize_z(value: str) -> str:
    """Replace a trailing `Z` with `+00:00` (3.10 fromisoformat rejects Z)."""
    if value.endswith("Z"):
        return value[:-1] + "+00:00"
    return value


def _parse_datetime_stdlib(value: str, offset: str | None) -> datetime.datetime:
    """Parse an admitted datetime segment with the stdlib.

    `offset` is unused here; the signature matches `_parse_datetime_accelerated`
    so `_parse_datetime` can select either.
    """
    return datetime.datetime.fromisoformat(_normalize_z(value))


def _parse_datetime_accelerated(value: str, offset: str | None) -> datetime.datetime:
    """Parse through ciso8601 where it answers exactly as the stdlib does.

    ciso8601 returns its own `FixedOffset` for a *numeric* offset where the
    stdlib returns a `datetime.timezone`, so `type(dt.tzinfo)` would depend on
    whether an optional package is installed. The accelerated path therefore
    takes only naive values and `Z`; `_DATETIME_RE` already captured the offset,
    so the split costs a group read rather than a second scan.
    """
    if offset is None:
        return _parse_datetime_ciso(value)
    return datetime.datetime.fromisoformat(_normalize_z(value))


_parse_datetime = _parse_datetime_accelerated if _HAS_CISO8601 else _parse_datetime_stdlib


class DateConverter(Converter):
    """Matches an ISO 8601 date; coerces to datetime.date."""

    __slots__ = ()
    #: A fixed ISO-8601 shape.
    specificity = 31

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept an ISO 8601 date segment; coerce to `datetime.date`."""
        if not _DATE_RE.match(value):
            return False, None
        try:
            return True, datetime.date.fromisoformat(value)
        except ValueError:
            return False, None


class DateTimeConverter(Converter):
    """Matches an ISO 8601 datetime; coerces to datetime.datetime."""

    __slots__ = ()
    #: A fixed ISO-8601 shape.
    specificity = 30

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept an ISO 8601 datetime segment; coerce to `datetime.datetime`."""
        matched = _DATETIME_RE.match(value)
        if matched is None:
            return False, None
        try:
            # Read off the module, so a test can swap the two parsers.
            return True, _parse_datetime(value, matched.group(1))
        except ValueError:
            return False, None


class TimeConverter(Converter):
    """Matches an ISO 8601 time; coerces to datetime.time."""

    __slots__ = ()
    #: A fixed ISO-8601 shape.
    specificity = 32

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept an ISO 8601 time segment; coerce to `datetime.time`."""
        if not _TIME_RE.match(value):
            return False, None
        try:
            return True, datetime.time.fromisoformat(_normalize_z(value))
        except ValueError:
            return False, None


class TimeDeltaConverter(Converter):
    """Matches an ISO 8601 duration or `str(timedelta)`; coerces to timedelta.

    A bare number such as `60` is rejected. An ISO duration (`P1DT2H`, at least
    one component) is accepted, as is Python's own `str(timedelta)` form so a
    real `timedelta` round-trips through `url_for`.
    """

    __slots__ = ()
    #: A fixed duration shape.
    specificity = 33

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept an ISO 8601 duration or `str(timedelta)` segment; coerce to `timedelta`."""
        m = _TIMEDELTA_RE.match(value)
        if m is not None:
            days, hours, minutes, seconds = m.groups()
            return True, datetime.timedelta(
                days=int(days) if days else 0,
                hours=int(hours) if hours else 0,
                minutes=int(minutes) if minutes else 0,
                seconds=float(seconds) if seconds else 0,
            )
        # A negative timedelta is `-1 day, 23:00:00`: signed days, unsigned clock.
        sm = _TIMEDELTA_STR_RE.match(value)
        if sm is None:
            return False, None
        return True, datetime.timedelta(
            days=int(sm["days"]) if sm["days"] else 0,
            hours=int(sm["hours"]),
            minutes=int(sm["minutes"]),
            seconds=float(sm["seconds"]),
        )


class DecimalConverter(Converter):
    """Matches a decimal literal; coerces to decimal.Decimal."""

    __slots__ = ()
    #: Accepts everything `float` does and more, so it sorts after it.
    specificity = 41

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept a decimal-literal segment; coerce to `decimal.Decimal`."""
        if not value or len(value) > _MAX_DECIMAL_CHARS or not _DECIMAL_RE.match(value):
            return False, None
        try:
            return True, decimal.Decimal(value)
        except decimal.InvalidOperation:
            return False, None
