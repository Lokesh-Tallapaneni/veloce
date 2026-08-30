"""A response header accessor reads, and clears, whatever casing is stored.

HTTP field names are case-insensitive (RFC 9110 Sec. 5.1), and `Response.headers`
is a plain dict, so an accessor spelled `self.headers.get(...)` only sees the
canonical key. `header_get` / `header_key` existed and were documented as the
authority; nine of the fifteen accessors did not use them, and the clear side
was worse - `expires = None` popped two spellings and reported success while a
third stayed on the wire.

`header_key` fast-paths the canonical key, so a header stored the usual way
costs the same single dict lookup it did before; only the previously-broken
casing pays a scan.

The one case knowingly left alone is `add_vary`'s fast path, pinned at the
bottom: making its guard case-insensitive means scanning whenever no `Vary` is
present, which is exactly when that path runs.
"""

from __future__ import annotations

import pytest

from tests._response_accessors import STRING_ACCESSORS as _ACCESSORS
from veloce import Response

_DATE = "Wed, 21 Oct 2015 07:28:00 GMT"


def _spellings(name: str) -> list[str]:
    """The canonical spelling plus two a client or middleware might store."""
    return [name, name.lower(), name.upper()]


@pytest.mark.parametrize(("attr", "header", "stored", "expected"), _ACCESSORS)
@pytest.mark.parametrize("case", [0, 1, 2])
def test_an_accessor_reads_any_casing(attr, header, stored, expected, case):
    spelling = _spellings(header)[case]
    response = Response(body=b"x", headers={spelling: stored})
    assert getattr(response, attr) == expected, spelling


@pytest.mark.parametrize("case", [0, 1, 2])
def test_the_date_accessor_reads_any_casing(case):
    response = Response(body=b"x", headers={_spellings("Date")[case]: _DATE})
    assert response.date is not None


@pytest.mark.parametrize("case", [0, 1, 2])
def test_cache_control_reads_any_casing(case):
    spelling = _spellings("Cache-Control")[case]
    response = Response(body=b"x", headers={spelling: "no-store"})
    assert response.cache_control.no_store is True, spelling


@pytest.mark.parametrize("case", [0, 1, 2])
def test_vary_and_allow_read_any_casing(case):
    vary = _spellings("Vary")[case]
    allow = _spellings("Allow")[case]
    response = Response(body=b"x", headers={vary: "Cookie", allow: "GET"})
    assert "Cookie" in list(response.vary), vary
    assert "GET" in list(response.allow), allow


# ── Clearing removes what the getter reported ────────────────────────


@pytest.mark.parametrize("attr", ["expires", "last_modified"])
@pytest.mark.parametrize("case", [0, 1, 2])
def test_clearing_a_date_header_removes_any_casing(attr, case):
    """The defect: the clear reported success and the header still shipped."""
    canonical = {"expires": "Expires", "last_modified": "Last-Modified"}[attr]
    spelling = _spellings(canonical)[case]
    response = Response(body=b"x", headers={spelling: _DATE})

    assert getattr(response, attr) is not None, spelling
    setattr(response, attr, None)

    assert getattr(response, attr) is None, spelling
    assert response.headers == {}, response.headers
    assert spelling.lower().encode() not in response.encode().lower()


@pytest.mark.parametrize("case", [0, 1, 2])
def test_setting_a_date_header_after_clearing_it_writes_once(case):
    response = Response(body=b"x", headers={_spellings("Expires")[case]: _DATE})
    response.expires = None
    response.expires = _DATE
    assert list(response.headers) == ["Expires"]


# ── add_vary merges under any spelling ──────────────────────


@pytest.mark.parametrize("spelling", ["Vary", "vary", "VARY", "vAry"])
def test_add_vary_merges_under_any_spelling(spelling):
    response = Response(body=b"x", headers={spelling: "Cookie"})
    merged = response.add_vary("Accept-Encoding")
    assert "Cookie" in merged
    assert "Accept-Encoding" in merged


def test_add_vary_no_longer_orphans_a_third_spelling():
    """This was pinned as a knowing exception; the reasoning behind it was wrong.

    The claim was that a third spelling merely produced two `Vary` field lines,
    which a recipient combines. Both emit paths fold duplicate field names and
    keep the last write, so the earlier value never reached the wire at all.
    """
    response = Response(body=b"x", headers={"VARY": "Cookie"})
    assert "Cookie" in list(response.vary)
    response.add_vary("Accept-Encoding")
    stored = [(k, v) for k, v in response.headers.items() if k.lower() == "vary"]
    assert len(stored) == 1
    assert set(stored[0][1].split(", ")) == {"Cookie", "Accept-Encoding"}
