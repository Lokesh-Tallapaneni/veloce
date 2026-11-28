"""The ASGI emit path memoises encoded headers without changing what it emits.

`_build_asgi_headers` encoded every header on every response: a `.lower()`, two
`_reject_header_crlf` calls (each calling `_header_value_has_crlf`, so four
Python-level calls per header), a name lookup and a latin-1 encode. A response
header is overwhelmingly one a middleware precomputed once, so the same two
`str` objects arrive every request and the whole scan can be one dict hit.

The risk a cache introduces is that it changes what is emitted, or that it grows
without bound when a value differs per response. Both are asserted here.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.app.asgi import (
    _ENCODED_HEADER_PAIRS,
    _MAX_ENCODED_PAIRS,
    _build_asgi_headers,
)
from veloce.middleware.security import SecurityHeadersMiddleware
from veloce.testclient import TestClient


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts from an empty cache and leaves one behind."""
    _ENCODED_HEADER_PAIRS.clear()
    yield
    _ENCODED_HEADER_PAIRS.clear()


# ── what it emits is unchanged ───────────────────────────────────────

CASES = [
    pytest.param({}, id="empty"),
    pytest.param({"X-Frame-Options": "DENY"}, id="one"),
    pytest.param(
        {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        },
        id="security-headers",
    ),
    pytest.param({"content-type": "application/json"}, id="content-type"),
    pytest.param({"Content-Length": "11"}, id="content-length"),
    pytest.param({"X-Odd": "café"}, id="non-ascii-value"),
    pytest.param({"X-Wide": "日本語"}, id="beyond-latin-1"),
]


@pytest.mark.parametrize("headers", CASES)
def test_a_second_call_emits_exactly_what_the_first_did(headers: dict):
    """The cache is a memo, so the hit must equal the miss."""
    first = _build_asgi_headers(dict(headers))
    second = _build_asgi_headers(dict(headers))
    assert first == second


@pytest.mark.parametrize("headers", CASES)
def test_the_emitted_bytes_do_not_depend_on_a_warm_cache(headers: dict):
    """Same input, cold cache versus warm: identical output."""
    warm = _build_asgi_headers(dict(headers))
    _ENCODED_HEADER_PAIRS.clear()
    cold = _build_asgi_headers(dict(headers))
    assert warm == cold


def test_a_repeated_name_still_collapses_to_one_header():
    """Case folding must survive memoisation: one field, not two."""
    emitted, _ct, _cl = _build_asgi_headers(
        {"Content-Security-Policy": "default-src 'self'", "content-security-policy": "none"}
    )
    names = [name for name, _value in emitted]
    assert names.count(b"content-security-policy") == 1
    assert emitted == [(b"content-security-policy", b"none")]


def test_set_cookie_is_never_memoised():
    """It is multi-valued and per-request; caching it would cross responses."""
    _build_asgi_headers({"Set-Cookie": "a=1; Path=/"})
    assert not [key for key in _ENCODED_HEADER_PAIRS if key[0].lower() == "set-cookie"]


def test_two_cookies_still_become_two_headers():
    emitted, _ct, _cl = _build_asgi_headers(
        {"Set-Cookie": "a=1; Path=/\r\nSet-Cookie: b=2; Path=/"}
    )
    assert [name for name, _v in emitted] == [b"set-cookie", b"set-cookie"]


# ── the guard the cache must not skip ────────────────────────────────


def test_a_crlf_value_is_still_rejected():
    with pytest.raises(ValueError, match="control character"):
        _build_asgi_headers({"X-Evil": "a\r\nInjected: 1"})


def test_a_crlf_name_is_still_rejected():
    with pytest.raises(ValueError, match="control character"):
        _build_asgi_headers({"X-Ev\ril": "ok"})


def test_a_nul_value_is_still_rejected():
    with pytest.raises(ValueError, match="control character"):
        _build_asgi_headers({"X-Evil": "a\x00b"})


def test_a_rejected_header_is_not_left_in_the_cache():
    """A value that raises must not be memoised as if it had passed."""
    with pytest.raises(ValueError):
        _build_asgi_headers({"X-Evil": "a\r\nInjected: 1"})
    assert ("X-Evil", "a\r\nInjected: 1") not in _ENCODED_HEADER_PAIRS


def test_the_guard_still_fires_on_a_later_call():
    """The first call populating the cache must not disarm the second."""
    _build_asgi_headers({"X-Frame-Options": "DENY"})
    with pytest.raises(ValueError, match="control character"):
        _build_asgi_headers({"X-Frame-Options": "DENY", "X-Evil": "a\nb"})


# ── it stays bounded ─────────────────────────────────────────────────


def test_a_per_response_value_cannot_grow_the_cache_without_bound():
    """An ETag differs every response; the cache must not track them all."""
    for i in range(_MAX_ENCODED_PAIRS * 3):
        _build_asgi_headers({"ETag": f'"{i}"'})
    assert len(_ENCODED_HEADER_PAIRS) <= _MAX_ENCODED_PAIRS


def test_the_cache_keeps_working_after_it_is_cleared():
    """Tripping the cap must not leave later responses wrong."""
    for i in range(_MAX_ENCODED_PAIRS * 2):
        _build_asgi_headers({"ETag": f'"{i}"'})
    emitted, _ct, _cl = _build_asgi_headers({"X-Frame-Options": "DENY"})
    assert emitted == [(b"x-frame-options", b"DENY")]


def test_the_cache_is_actually_used():
    """A memo that never hits would make the whole change pointless."""
    headers = {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}
    _build_asgi_headers(dict(headers))
    size_after_first = len(_ENCODED_HEADER_PAIRS)
    assert size_after_first == 2
    _build_asgi_headers(dict(headers))
    assert len(_ENCODED_HEADER_PAIRS) == size_after_first


# ── end to end, through a real app ───────────────────────────────────


def test_repeated_requests_carry_identical_middleware_headers():
    app = Veloce(openapi_url=None)
    app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=31536000))

    @app.get("/j")
    async def j() -> dict:
        return {"ok": True}

    client = TestClient(app)
    first = dict(client.get("/j").headers)
    second = dict(client.get("/j").headers)
    assert first == second
    assert first["x-frame-options"] == "DENY"
    assert first["strict-transport-security"] == "max-age=31536000"


def test_two_apps_with_different_policies_do_not_share_a_value():
    """The key is (name, value), so one app's policy cannot leak into another's."""
    strict = Veloce(openapi_url=None)
    strict.add_middleware(SecurityHeadersMiddleware(frame_options="DENY"))
    loose = Veloce(openapi_url=None)
    loose.add_middleware(SecurityHeadersMiddleware(frame_options="SAMEORIGIN"))

    for app in (strict, loose):

        @app.get("/j")
        async def j() -> dict:
            return {"ok": True}

    assert TestClient(strict).get("/j").headers["x-frame-options"] == "DENY"
    assert TestClient(loose).get("/j").headers["x-frame-options"] == "SAMEORIGIN"
    assert TestClient(strict).get("/j").headers["x-frame-options"] == "DENY"
