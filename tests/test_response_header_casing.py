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

from veloce import Response

_DATE = "Wed, 21 Oct 2015 07:28:00 GMT"

#: `(accessor, canonical header, stored value, expected read)`.
_ACCESSORS = [
    ("www_authenticate", "WWW-Authenticate", 'Bearer realm="api"', 'Bearer realm="api"'),
    ("content_encoding", "Content-Encoding", "gzip", "gzip"),
    ("content_language", "Content-Language", "en", "en"),
    ("accept_ranges", "Accept-Ranges", "bytes", "bytes"),
    ("content_range", "Content-Range", "bytes 0-1/2", "bytes 0-1/2"),
    ("location", "Location", "/next", "/next"),
    ("content_location", "Content-Location", "/here", "/here"),
    ("age", "Age", "12", 12),
    ("retry_after", "Retry-After", "30", 30),
]


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


# ── The knowing exception, pinned so a change is deliberate ──────────


@pytest.mark.parametrize("spelling", ["Vary", "vary"])
def test_add_vary_merges_under_the_two_spellings_it_guards(spelling):
    response = Response(body=b"x", headers={spelling: "Cookie"})
    merged = response.add_vary("Accept-Encoding")
    assert "Cookie" in merged
    assert "Accept-Encoding" in merged


def test_add_vary_knowingly_does_not_probe_a_third_spelling():
    """Documented, measured, and deliberate - not an oversight.

    A case-insensitive guard would scan the header dict whenever no `Vary` is
    present, which is precisely when this fast path runs: 116 ns to 521 ns per
    call on a four-header response, about 3% of a request. Framework code
    reaches `Vary` through `add_vary` or the canonical constant.

    The property still reports what is actually stored, which is the half that
    was worth fixing.
    """
    response = Response(body=b"x", headers={"VARY": "Cookie"})
    # The getter is honest about it...
    assert "Cookie" in list(response.vary)
    # ...while the fast path writes alongside rather than merging.
    response.add_vary("Accept-Encoding")
    assert response.headers["VARY"] == "Cookie"
    assert response.headers["Vary"] == "Accept-Encoding"
