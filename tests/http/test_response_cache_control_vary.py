"""Response.set_cache_control + Response.add_vary."""

from __future__ import annotations

from veloce import Response

# ── set_cache_control ────────────────────────────────────────────────


def test_max_age_only():
    resp = Response()
    val = resp.set_cache_control(max_age=3600)
    assert val == "max-age=3600"
    assert resp.headers["Cache-Control"] == "max-age=3600"


def test_public_max_age_combined():
    resp = Response()
    val = resp.set_cache_control(max_age=600, public=True)
    assert val == "public, max-age=600"
    assert resp.headers["Cache-Control"] == "public, max-age=600"


def test_no_store_overrides_caching():
    resp = Response()
    resp.set_cache_control(no_store=True)
    assert resp.headers["Cache-Control"] == "no-store"


def test_immutable_and_max_age_for_static_assets():
    """Common static-asset pattern: long max-age + immutable."""
    resp = Response()
    resp.set_cache_control(max_age=31536000, immutable=True, public=True)
    val = resp.headers["Cache-Control"]
    assert "public" in val
    assert "immutable" in val
    assert "max-age=31536000" in val


def test_s_maxage_for_shared_caches():
    resp = Response()
    resp.set_cache_control(max_age=60, s_maxage=600)
    val = resp.headers["Cache-Control"]
    assert "max-age=60" in val
    assert "s-maxage=600" in val


def test_no_directives_does_not_write_header():
    """A no-op call doesn't pollute the headers."""
    resp = Response()
    val = resp.set_cache_control()
    assert val == ""
    assert "Cache-Control" not in resp.headers


# ── add_vary ─────────────────────────────────────────────────────────


def test_add_vary_writes_new_header():
    resp = Response()
    val = resp.add_vary("Accept")
    assert val == "Accept"
    assert resp.headers["Vary"] == "Accept"


def test_add_vary_appends_without_dupes():
    resp = Response()
    resp.add_vary("Accept")
    val = resp.add_vary("Accept-Language")
    assert val == "Accept, Accept-Language"


def test_add_vary_dedupe_case_insensitive():
    resp = Response()
    resp.add_vary("Accept")
    val = resp.add_vary("accept")  # case-insensitive duplicate
    assert val == "Accept"


def test_add_vary_multiple_at_once():
    resp = Response()
    val = resp.add_vary("Origin", "Accept-Encoding")
    assert val == "Origin, Accept-Encoding"


def test_add_vary_normalises_lowercase_existing():
    """A pre-existing lower-case `vary` gets canonicalised on next add."""
    resp = Response()
    resp.headers["vary"] = "Cookie"
    resp.add_vary("Origin")
    assert "vary" not in resp.headers
    assert resp.headers["Vary"] == "Cookie, Origin"


def test_add_vary_fast_path_clears_empty_lowercase_vary():
    """The no-existing-`Vary`, single-name fast path still clears an empty
    lower-case `vary` key so the response carries exactly one canonical header.
    """
    resp = Response()
    resp.headers["vary"] = ""  # empty -> treated as "no existing Vary"
    val = resp.add_vary("Cookie")
    assert val == "Cookie"
    assert "vary" not in resp.headers
    assert resp.headers["Vary"] == "Cookie"
