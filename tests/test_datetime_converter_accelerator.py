"""Why `<datetime:>` still parses with the stdlib, and what it guarantees.

`ciso8601` parses ISO 8601 in C and is the obvious accelerator for this
converter. It was measured against the stdlib on CPython 3.12, in three
implementations, and none of them is an improvement worth taking:

    variant                     naive     Z    +05:30   behaviour
    raw ciso8601                 2.9x   3.9x    3.2x    tzinfo type differs
    + tzinfo normalisation       2.1x   2.5x    0.39x   identical
    + shape-based dispatch      0.76x   1.05x   0.54x   identical

The raw swap is the only real acceleration, and it is not behaviour-preserving:
ciso8601 returns its own `FixedOffset` for a non-UTC offset where the stdlib
returns `datetime.timezone`. The datetimes compare equal and render the same
`isoformat()`, but `type(dt.tzinfo)` and `dt.tzinfo ==` differ - and they would
differ according to whether an optional package happened to be installed, which
is worse than either behaviour chosen deliberately.

Normalising the tzinfo removes that difference and costs more than it saves on
offset values: the expense is `datetime.replace`, not constructing the timezone,
so caching timezone objects by offset (tried, measured) does not recover it.
Dispatching on the input shape to send only the identical cases through
ciso8601 costs more in string work than the parse it avoids.

So the converter keeps `fromisoformat`. Recorded here rather than left as
folklore, because "use ciso8601, it's 10x faster" is a reasonable thing for
someone to propose again - that figure is against `strptime`, not against a
modern `fromisoformat`.

What this file does test is the property that made the swap look safe in the
first place, and which is worth pinning whoever parses: the *parser is not the
gate*. `_DATETIME_RE` decides what the converter accepts, and a route match is
decided by that regex. Anything a more permissive parser would take - an ordinal
date, a lowercase `z` - never reaches the parser at all.
"""

from __future__ import annotations

import datetime

import pytest

from veloce import Veloce
from veloce.routing import converters
from veloce.testclient import TestClient

ACCEPTED = [
    "2026-08-26T12:30:00",
    "2026-08-26T12:30:00Z",
    "2026-08-26T12:30:00+05:30",
    "2026-08-26T12:30:00-08:00",
    "2026-08-26T12:30:00.123456",
    "2026-08-26T12:30",
    "2026-08-26 12:30:00",
    "2026-01-01T00:00:00",
    "2026-12-31T23:59:59",
]

REJECTED = [
    "not-a-date",
    "",
    "2026-13-01T00:00:00",
    "2026-08-26T25:00:00",
    "2026-08-26T12:60:00",
    "2026-240",
    "2026-08-26T12:30:00z",
    "2026-W35-3",
    "20260826T123000",
]


# ── what the converter accepts ───────────────────────────────────────


@pytest.mark.parametrize("value", ACCEPTED)
def test_an_accepted_value_matches_and_coerces(value: str):
    matched, parsed = converters.DateTimeConverter().match(value)
    assert matched is True
    assert isinstance(parsed, datetime.datetime)


@pytest.mark.parametrize("value", REJECTED)
def test_a_rejected_value_does_not_match(value: str):
    assert converters.DateTimeConverter().match(value)[0] is False


@pytest.mark.parametrize("value", ACCEPTED)
def test_the_parsed_value_equals_the_stdlib_parse(value: str):
    expected = datetime.datetime.fromisoformat(converters._normalize_z(value))
    assert converters.DateTimeConverter().match(value)[1] == expected


def test_a_z_suffix_becomes_utc():
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26T12:30:00Z")
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_an_offset_is_preserved():
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26T12:30:00+05:30")
    assert parsed.utcoffset() == datetime.timedelta(hours=5, minutes=30)


def test_the_tzinfo_is_a_stdlib_timezone():
    """The property a C accelerator would have quietly changed."""
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26T12:30:00+05:30")
    assert type(parsed.tzinfo) is datetime.timezone


def test_microseconds_survive():
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26T12:30:00.123456")
    assert parsed.microsecond == 123456


def test_a_naive_value_stays_naive():
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26T12:30:00")
    assert parsed.tzinfo is None


# ── the prefilter is the gate, not the parser ────────────────────────


def test_the_regex_admits_nothing_the_parser_would_widen():
    """An ordinal date and a lowercase `z` are what a laxer parser would add."""
    assert converters._DATETIME_RE.match("2026-240") is None
    assert converters._DATETIME_RE.match("2026-08-26T12:30:00z") is None


def test_an_ordinal_date_does_not_match():
    assert converters.DateTimeConverter().match("2026-240")[0] is False


def test_a_lowercase_z_does_not_match():
    assert converters.DateTimeConverter().match("2026-08-26T12:30:00z")[0] is False


def test_every_accepted_value_passes_the_prefilter_first():
    """The gate must not be doing less work than the parser behind it."""
    for value in ACCEPTED:
        assert converters._DATETIME_RE.match(value) is not None


def test_a_value_the_prefilter_admits_but_the_parser_rejects_is_a_miss():
    """The regex is shape-only, so the parse still has to agree."""
    assert converters._DATETIME_RE.match("2026-13-01T00:00:00") is not None
    assert converters.DateTimeConverter().match("2026-13-01T00:00:00")[0] is False


# ── the neighbouring converters ──────────────────────────────────────


def test_the_date_converter_coerces():
    matched, parsed = converters.DateConverter().match("2026-08-26")
    assert matched is True
    assert parsed == datetime.date(2026, 8, 26)


def test_the_date_converter_rejects_a_datetime():
    assert converters.DateConverter().match("2026-08-26T12:30:00")[0] is False


def test_the_time_converter_coerces():
    matched, parsed = converters.TimeConverter().match("12:30:00")
    assert matched is True
    assert parsed == datetime.time(12, 30)


# ── end to end through real routing ──────────────────────────────────


def _routed_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/at/{when:datetime}")
    async def at(when: datetime.datetime) -> dict:
        return {"iso": when.isoformat()}

    @app.get("/on/{day:date}")
    async def on(day: datetime.date) -> dict:
        return {"iso": day.isoformat()}

    return app


@pytest.mark.parametrize("value", ACCEPTED)
def test_a_datetime_segment_routes(value: str):
    from urllib.parse import quote

    response = TestClient(_routed_app()).get(f"/at/{quote(value)}")
    assert response.status_code == 200
    expected = datetime.datetime.fromisoformat(converters._normalize_z(value))
    assert response.json()["iso"] == expected.isoformat()


@pytest.mark.parametrize("value", ["not-a-date", "2026-13-01T00:00:00", "2026-240"])
def test_a_bad_datetime_segment_does_not_route(value: str):
    from urllib.parse import quote

    assert TestClient(_routed_app()).get(f"/at/{quote(value)}").status_code == 404


def test_a_date_segment_routes():
    assert TestClient(_routed_app()).get("/on/2026-08-26").json() == {"iso": "2026-08-26"}


def test_repeated_requests_are_stable():
    client = TestClient(_routed_app())
    for _ in range(20):
        assert client.get("/at/2026-08-26T12:30:00Z").status_code == 200
