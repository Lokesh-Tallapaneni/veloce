"""`check_preconditions` enforces `If-Unmodified-Since`, as `StaticFiles` does.

RFC 9110 gives the write-side preconditions a precedence order (Sec. 13.2.2):
`If-Match` first, and `If-Unmodified-Since` only when `If-Match` is absent. Both
guard against the lost update — a client says "only write if the thing has not
changed since I read it", by ETag or by date.

Veloce had two implementations. `contrib/staticfiles._precondition_failed`
honoured both. `Response.check_preconditions` — the one a handler calls, and the
one named as though it checked the preconditions — honoured only `If-Match`:

    PUT /doc  If-Unmodified-Since: <a date in the past>   ->  200

A client sending a date rather than an ETag got no protection at all, silently,
from a method whose whole purpose is to provide it. `StaticFiles` got it right,
so the two doors disagreed about what a precondition is.

These tests assert the handler-facing method against the RFC's precedence, and
pin the cases where refusing would be wrong — an absent header, an unevaluable
comparison, and a resource that has *not* been modified.
"""

from __future__ import annotations

import pytest

from veloce import JSONResponse, Veloce
from veloce.contrib.staticfiles import _precondition_failed
from veloce.testclient import TestClient

_PAST = "Mon, 01 Jan 2001 00:00:00 GMT"
_MODIFIED = "Wed, 21 Oct 2026 07:28:00 GMT"
_FUTURE = "Fri, 01 Jan 2100 00:00:00 GMT"


def _client(*, etag: str | None = 'W/"v1"', last_modified: str | None = _MODIFIED):
    app = Veloce(openapi_url=None)

    @app.put("/doc")
    async def doc(request):
        resp = JSONResponse({"written": True})
        if etag is not None:
            resp.headers["ETag"] = etag
        if last_modified is not None:
            resp.headers["Last-Modified"] = last_modified
        return resp.check_preconditions(request)

    return TestClient(app)


def _put(client, **headers):
    return client.put("/doc", headers=headers).status_code


# ── If-Unmodified-Since ──────────────────────────────────────────────


def test_a_stale_if_unmodified_since_is_refused():
    """The defect: this was a 200."""
    assert _put(_client(), **{"If-Unmodified-Since": _PAST}) == 412


def test_a_current_if_unmodified_since_is_allowed():
    """The negative that matters most: a guard that refused everything would be
    worse than one that refused nothing."""
    assert _put(_client(), **{"If-Unmodified-Since": _FUTURE}) == 200


def test_an_exactly_equal_if_unmodified_since_is_allowed():
    """ "Not modified since" includes "modified exactly then" (Sec. 13.1.4)."""
    assert _put(_client(), **{"If-Unmodified-Since": _MODIFIED}) == 200


def test_no_precondition_header_is_allowed():
    assert _put(_client()) == 200


def test_an_unparseable_if_unmodified_since_is_allowed():
    """An unparseable date is treated as absent, not as a refusal."""
    assert _put(_client(), **{"If-Unmodified-Since": "not-a-date"}) == 200


def test_a_response_with_no_last_modified_cannot_be_evaluated():
    """Nothing to compare against is not a reason to refuse the write."""
    assert _put(_client(last_modified=None), **{"If-Unmodified-Since": _PAST}) == 200


# ── the precedence order ─────────────────────────────────────────────


def test_if_match_takes_precedence_when_it_passes():
    """Sec. 13.2.2: with `If-Match` present, the date is not evaluated at all -
    so a stale date alongside a passing `If-Match` must still be allowed."""
    assert _put(_client(), **{"If-Match": "*", "If-Unmodified-Since": _PAST}) == 200


def test_if_match_takes_precedence_when_it_fails():
    status = _put(_client(), **{"If-Match": '"other"', "If-Unmodified-Since": _FUTURE})
    assert status == 412


# ── If-Match, unchanged ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("*", 200),
        ('"other"', 412),
        ('W/"v1"', 412),
    ],
)
def test_if_match_behaviour_is_unchanged(header, expected):
    """`W/"v1"` is a *weak* tag and `If-Match` mandates the strong comparison
    (Sec. 8.8.3.1), so it fails closed - as it did before."""
    assert _put(_client(), **{"If-Match": header}) == expected


# ── the two implementations agree ────────────────────────────────────


def _static_verdict(if_match, if_unmodified_since, etag, mtime):

    return _precondition_failed(if_match, if_unmodified_since, etag, mtime)


@pytest.mark.parametrize(
    ("if_match", "since_offset", "expected_failed"),
    [
        ((), None, False),
        ((), +100.0, False),
        ((), -100.0, True),
        (("*",), -100.0, False),
        (('"other"',), None, True),
    ],
    ids=["nothing", "current-date", "stale-date", "star-wins", "bad-etag"],
)
def test_the_static_implementation_agrees_with_the_handler_one(
    if_match, since_offset, expected_failed
):
    """Both doors, one rule. Asserted at the static side against the same table
    the handler side is asserted against above."""
    mtime = 1_000_000.0
    since = None if since_offset is None else mtime + since_offset
    assert _static_verdict(if_match, since, 'W/"v1"', mtime) is expected_failed


def test_both_doors_refuse_a_stale_date():
    """The specific disagreement this finding is about, stated once more as a
    single assertion over both."""
    assert _static_verdict((), 900_000.0, 'W/"v1"', 1_000_000.0) is True
    assert _put(_client(), **{"If-Unmodified-Since": _PAST}) == 412


def test_both_doors_allow_a_current_date():
    assert _static_verdict((), 1_100_000.0, 'W/"v1"', 1_000_000.0) is False
    assert _put(_client(), **{"If-Unmodified-Since": _FUTURE}) == 200
