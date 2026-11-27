"""`<datetime:>` parses through ciso8601 where that is indistinguishable.

ciso8601 parses the same shapes in C, and handles a trailing `Z` itself, so the
stdlib's `Z`-to-`+00:00` rewrite disappears along with the call. Measured over
the whole converter - prefilter regex plus parse - on CPython 3.12:

    '2026-08-26T12:30:00'         854 ns -> 585 ns   1.46x
    '2026-08-26T12:30:00Z'       1037 ns -> 615 ns   1.68x
    '2026-08-26 12:30:00'         848 ns -> 595 ns   1.42x
    '2026-08-26T12:30:00+05:30'   985 ns -> 979 ns   1.01x
    '2026-08-26T12:30:00+00:00'   946 ns -> 923 ns   1.02x

**Why it is only some of the values.** The two parsers do not agree on
everything. For a *numeric* offset ciso8601 returns its own `FixedOffset` where
the stdlib returns `datetime.timezone`: the datetimes compare equal and render
the same `isoformat()`, but `type(dt.tzinfo)` differs - and would differ
according to whether an optional package happened to be installed. Three
implementations were measured before this one:

    variant                     naive     Z    +05:30   behaviour
    raw ciso8601                 2.9x   3.9x    3.2x    tzinfo type differs
    + tzinfo normalisation       2.1x   2.5x    0.39x   identical
    + string-shape dispatch      0.76x  1.05x   0.54x   identical

Normalising the tzinfo costs more than it saves, because the expense is
`datetime.replace` rather than constructing the timezone - caching timezone
objects by offset was tried and measured and does not recover it. Re-scanning
the string to decide costs more than the parse it avoids.

What works is not re-deciding at all: `_DATETIME_RE` has already scanned past
the offset, so it captures it, and the converter reads the group. Values with no
numeric offset go to ciso8601; the rest take exactly the call this converter has
always made - which also keeps 3.10's `Z` rewrite, since `fromisoformat` there
rejects `Z` outright.

Fuzzed across 13,824 strings the prefilter admits, the accelerated and stdlib
paths agreed on every one - acceptance, value, tzinfo type and tzinfo equality -
and no value carrying a non-UTC offset ever reached ciso8601.

Without the package the converter behaves exactly as before; `ciso8601` is an
optional extra.
"""

from __future__ import annotations

import builtins
import datetime
from unittest import mock

import pytest

from veloce import Veloce
from veloce.routing import converters
from veloce.testclient import TestClient

ACCEPTED = [
    "2026-08-26T12:30:00",
    "2026-08-26T12:30:00Z",
    "2026-08-26T12:30:00+05:30",
    "2026-08-26T12:30:00-08:00",
    "2026-08-26T12:30:00+00:00",
    "2026-08-26T12:30:00.123456",
    "2026-08-26T12:30:00.123456Z",
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
    "2026-08-26",
]


def _stdlib(value: str):
    """What the converter answers with the accelerator absent."""
    matched = converters._DATETIME_RE.match(value)
    if matched is None:
        return False, None
    try:
        return True, converters._parse_datetime_stdlib(value, matched.group(1))
    except ValueError:
        return False, None


# ── the accelerator is wired in ──────────────────────────────────────


def test_ciso8601_is_used_when_installed():
    pytest.importorskip("ciso8601")
    assert converters._HAS_CISO8601 is True
    assert converters._parse_datetime is converters._parse_datetime_accelerated


def test_the_parser_is_chosen_once_rather_than_per_request():
    """A per-call import or probe would spend the gain it was added for.

    This asserted `callable(converters._parse_datetime)`, which holds for either
    parser and equally for one that re-probes for `ciso8601` on every call - the
    regression the test is named after. The choice is made once, at module level
    in `converters.py`, so a conversion must import nothing at all.
    """
    chosen = converters._parse_datetime
    imported: list[str] = []
    real_import = builtins.__import__

    def recording_import(name, *args, **kwargs):
        imported.append(name)
        return real_import(name, *args, **kwargs)

    converter = converters.DateTimeConverter()
    with mock.patch.object(builtins, "__import__", recording_import):
        for value in ("2026-08-26T12:30:00", "2026-08-26T12:30:00Z"):
            for _ in range(3):
                assert converter.match(value)[0] is True

    assert imported == [], f"converting imported {imported}"
    assert converters._parse_datetime is chosen


def test_the_prefilter_captures_the_offset():
    """The split is a group read, not a second scan of the string."""
    assert converters._DATETIME_RE.match("2026-08-26T12:30:00+05:30").group(1) == "+05:30"
    assert converters._DATETIME_RE.match("2026-08-26T12:30:00Z").group(1) is None
    assert converters._DATETIME_RE.match("2026-08-26T12:30:00").group(1) is None


def test_a_value_with_no_offset_takes_the_accelerated_path(monkeypatch):
    pytest.importorskip("ciso8601")
    seen: list[str] = []
    monkeypatch.setattr(
        converters,
        "_parse_datetime_ciso",
        lambda v: seen.append(v) or datetime.datetime(2026, 1, 1),
    )
    converters._parse_datetime_accelerated("2026-08-26T12:30:00", None)
    assert seen == ["2026-08-26T12:30:00"]


def test_a_value_with_an_offset_does_not_reach_ciso8601(monkeypatch):
    """The whole basis of the split: an offset must never go through it."""
    pytest.importorskip("ciso8601")
    monkeypatch.setattr(
        converters,
        "_parse_datetime_ciso",
        lambda v: pytest.fail(f"offset value {v!r} reached ciso8601"),
    )
    parsed = converters._parse_datetime_accelerated("2026-08-26T12:30:00+05:30", "+05:30")
    assert parsed.utcoffset() == datetime.timedelta(hours=5, minutes=30)


# ── positive: values arrive correctly ────────────────────────────────


@pytest.mark.parametrize("value", ACCEPTED)
def test_an_accepted_value_matches_and_coerces(value: str):
    matched, parsed = converters.DateTimeConverter().match(value)
    assert matched is True
    assert isinstance(parsed, datetime.datetime)


def test_a_z_suffix_becomes_utc():
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26T12:30:00Z")
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_an_offset_is_preserved():
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26T12:30:00+05:30")
    assert parsed.utcoffset() == datetime.timedelta(hours=5, minutes=30)


def test_a_negative_offset_is_preserved():
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26T12:30:00-08:00")
    assert parsed.utcoffset() == datetime.timedelta(hours=-8)


def test_microseconds_survive():
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26T12:30:00.123456")
    assert parsed.microsecond == 123456


def test_microseconds_survive_with_a_z_suffix():
    """The accelerated path, with the fractional part attached."""
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26T12:30:00.123456Z")
    assert parsed.microsecond == 123456
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_a_naive_value_stays_naive():
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26T12:30:00")
    assert parsed.tzinfo is None


def test_a_space_separator_is_accepted():
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26 12:30:00")
    assert parsed.hour == 12


# ── negative: what must still be refused ─────────────────────────────


@pytest.mark.parametrize("value", REJECTED)
def test_a_rejected_value_does_not_match(value: str):
    assert converters.DateTimeConverter().match(value)[0] is False


def test_an_ordinal_date_is_refused_though_ciso8601_would_take_it():
    """A disagreement that would otherwise have leaked into routing."""
    ciso8601 = pytest.importorskip("ciso8601")
    assert ciso8601.parse_datetime("2026-240")
    assert converters.DateTimeConverter().match("2026-240")[0] is False


def test_a_lowercase_z_is_refused_though_ciso8601_would_take_it():
    ciso8601 = pytest.importorskip("ciso8601")
    assert ciso8601.parse_datetime("2026-08-26T12:30:00z")
    assert converters.DateTimeConverter().match("2026-08-26T12:30:00z")[0] is False


def test_a_value_the_prefilter_admits_but_the_parser_rejects_is_a_miss():
    """The regex is shape-only, so the parse still has to agree."""
    assert converters._DATETIME_RE.match("2026-13-01T00:00:00") is not None
    assert converters.DateTimeConverter().match("2026-13-01T00:00:00")[0] is False


# ── the two paths answer identically, which is the safety property ───


@pytest.mark.parametrize("value", ACCEPTED + REJECTED)
def test_the_accelerated_and_stdlib_paths_agree(value: str):
    assert converters.DateTimeConverter().match(value) == _stdlib(value)


@pytest.mark.parametrize("value", ACCEPTED)
def test_the_tzinfo_type_is_the_same_either_way(value: str):
    """The property the raw swap would have changed."""
    _matched, parsed = converters.DateTimeConverter().match(value)
    _expected_matched, expected = _stdlib(value)
    assert type(parsed.tzinfo) is type(expected.tzinfo)
    assert parsed.tzinfo == expected.tzinfo


@pytest.mark.parametrize("value", ACCEPTED)
def test_an_offset_value_yields_a_stdlib_timezone(value: str):
    _matched, parsed = converters.DateTimeConverter().match(value)
    assert parsed.tzinfo is None or type(parsed.tzinfo) is datetime.timezone


# The grid the accelerated and stdlib parsers must agree across. Deliberately
# includes month 13, day 32 and hour 25: `_DATETIME_RE` is a *shape* gate, not
# a validity check, so it admits all of these and agreeing on refusing them is
# as much the property as agreeing on accepting the rest.
GRID = [
    f"{year}-{month}-{day}{sep}{time_part}{zone}"
    for year in ("2026", "0001", "9999")
    for month in ("01", "02", "12", "13")
    for day in ("01", "28", "31", "32")
    for sep in ("T", " ")
    for time_part in ("12:30:00", "25:00:00", "12:30", "12:30:00.123456")
    for zone in ("", "Z", "+00:00", "-00:00", "+05:30", "-08:00")
]


def test_the_gate_admits_the_whole_grid():
    """It is a shape check. The `continue` this replaced never once fired.

    The loop it guarded carried `assert admitted > 1000` with no explanation;
    the real number is every value in the grid, which is what makes the
    agreement below a statement about all of them rather than an unknown
    subset.
    """
    assert len(GRID) == 2304
    assert [v for v in GRID if converters._DATETIME_RE.match(v) is None] == []


def test_the_two_paths_agree_across_the_whole_grid():
    """The fuzz that justified the split, run rather than asserted.

    Every disagreement is reported, not just the first: a parser change that
    breaks one shape usually breaks a family of them, and the family is the
    useful diagnostic.
    """
    converter = converters.DateTimeConverter()
    disagreements = [value for value in GRID if converter.match(value) != _stdlib(value)]
    assert disagreements == [], f"{len(disagreements)} values parse differently"


def test_the_grid_exercises_both_outcomes():
    """Both paths refusing everything would satisfy the agreement above."""
    converter = converters.DateTimeConverter()
    outcomes = {converter.match(value)[0] for value in GRID}
    assert outcomes == {True, False}


# ── the fallback, for an environment without the package ─────────────


def test_the_fallback_answers_identically(monkeypatch):
    """An app must not route differently for having installed a package."""
    converter = converters.DateTimeConverter()
    accelerated = [converter.match(v) for v in ACCEPTED + REJECTED]

    monkeypatch.setattr(converters, "_parse_datetime", converters._parse_datetime_stdlib)
    fallback = [converter.match(v) for v in ACCEPTED + REJECTED]

    assert accelerated == fallback


def test_the_fallback_still_normalises_z(monkeypatch):
    """3.10's `fromisoformat` rejects `Z`, so the rewrite has to stay."""
    monkeypatch.setattr(converters, "_parse_datetime", converters._parse_datetime_stdlib)
    _matched, parsed = converters.DateTimeConverter().match("2026-08-26T12:30:00Z")
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_the_fallback_rejects_the_same_values(monkeypatch):
    monkeypatch.setattr(converters, "_parse_datetime", converters._parse_datetime_stdlib)
    for value in REJECTED:
        assert converters.DateTimeConverter().match(value)[0] is False


# ── the neighbouring converters are untouched ────────────────────────


def test_the_date_converter_still_uses_the_stdlib():
    """ciso8601 has no date-only parser; through it a date measured 1.19x."""
    matched, parsed = converters.DateConverter().match("2026-08-26")
    assert matched is True
    assert parsed == datetime.date(2026, 8, 26)


def test_the_date_converter_rejects_a_datetime():
    assert converters.DateConverter().match("2026-08-26T12:30:00")[0] is False


def test_the_time_converter_is_unaffected():
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
    assert response.json()["iso"] == _stdlib(value)[1].isoformat()


@pytest.mark.parametrize("value", ["not-a-date", "2026-13-01T00:00:00", "2026-240"])
def test_a_bad_datetime_segment_does_not_route(value: str):
    from urllib.parse import quote

    assert TestClient(_routed_app()).get(f"/at/{quote(value)}").status_code == 404


def test_a_date_segment_routes():
    assert TestClient(_routed_app()).get("/on/2026-08-26").json() == {"iso": "2026-08-26"}


def test_the_parser_does_not_drift_across_requests():
    """Generated once, reused; a stateful parser bug would drift."""
    client = TestClient(_routed_app())
    for _ in range(20):
        assert client.get("/at/2026-08-26T12:30:00Z").status_code == 200
        assert client.get("/at/2026-08-26T12:30:00%2B05:30").status_code == 200
