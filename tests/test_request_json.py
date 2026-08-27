"""Request body-as-JSON accessors.

`Request.json()` is async (the ASGI convention; exercised via
`await request.json()`).
`Request.get_json(force=, silent=, cache=)` is the synchronous alias.
"""

from __future__ import annotations

import inspect

import pytest

import veloce.http.request as reqmod
from tests.conftest import make_request
from veloce import Request
from veloce.exceptions import BadRequest


def _req(body: bytes = b"", content_type: str = "application/json") -> Request:
    return make_request(
        method="POST",
        path="/x",
        query_string="",
        headers={"content-type": content_type} if content_type else {},
        body=body,
    )


# ── Happy path ───────────────────────────────────────────────────────


def test_parses_json_body():
    assert _req(b'{"a": 1}').get_json() == {"a": 1}


def test_empty_body_returns_none():
    assert _req(b"").get_json() is None


def test_non_json_content_type_returns_none():
    """Default behaviour: refuse non-JSON content types."""
    assert _req(b'{"a": 1}', content_type="text/plain").get_json() is None


# ── force ────────────────────────────────────────────────────────────


def test_force_skips_content_type_check():
    assert _req(b'{"a": 1}', content_type="text/plain").get_json(force=True) == {"a": 1}


def test_force_still_returns_none_for_empty_body():
    assert _req(b"", content_type="text/plain").get_json(force=True) is None


# ── silent ───────────────────────────────────────────────────────────


def test_default_raises_on_malformed_json():
    # Malformed JSON now raises BadRequest (400) with a stable, body-independent
    # message instead of the raw decoder error - the sync path matches the async
    # one and no decoder internals leak into the production response.

    with pytest.raises(BadRequest) as exc:
        _req(b"not-json").get_json()
    assert exc.value.detail == "Invalid JSON body"


def test_silent_swallows_parse_error():
    assert _req(b"not-json").get_json(silent=True) is None


# ── cache ────────────────────────────────────────────────────────────


def test_cache_default_returns_same_object():
    req = _req(b'{"a": 1}')
    first = req.get_json()
    second = req.get_json()
    assert first is second  # identity, not just equality


def test_cache_false_reparses():
    req = _req(b'{"a": 1}')
    first = req.get_json()
    second = req.get_json(cache=False)
    assert first == second
    # Different object identity proves the re-parse happened.
    assert first is not second


# ── async Request.json() — pins issue #74 ─────────────────────────────


def test_request_json_is_coroutine_function():
    """`Request.json` must remain `async def`. A future contributor reverting
    it to a sync method would break the `await request.json()` idiom;
    this is the regression guard."""
    assert inspect.iscoroutinefunction(Request.json)


def test_request_get_json_stays_synchronous():
    """The synchronous `get_json()` keeps the sync signature so callers
    that prefer a sync API do not need to rewrite their code."""
    assert not inspect.iscoroutinefunction(Request.get_json)


async def test_request_json_awaits_to_parsed_value():
    """End-to-end of the async API: `await request.json()` resolves to
    the parsed dict."""
    assert await _req(b'{"a": 1}').json() == {"a": 1}


async def test_request_json_empty_body_resolves_to_none():
    """Empty body hits the `if self.body` short-circuit and returns
    `None` rather than raising. Pins the branch the inspector flagged."""
    assert await _req(b"").json() is None


def test_get_json_raises_when_body_not_yet_buffered():
    """The sync `get_json()` accessor must refuse to parse until the body
    has been drained — it points the caller at `await request.json()`.
    Pins the contract before Pass 2 makes the un-drained branch reachable."""
    req = _req(b'{"a": 1}')
    req._body_drained = False
    with pytest.raises(RuntimeError, match=r"await request\.json\(\)"):
        req.get_json()


# ── JSON `null` body is cached, not re-parsed every call ──────────────


def _count_loads(monkeypatch) -> dict:
    """Patch the module's orjson.loads with a call counter; return the counter."""

    calls = {"n": 0}
    real = reqmod.orjson.loads

    def counting(raw):
        calls["n"] += 1
        return real(raw)

    monkeypatch.setattr(reqmod.orjson, "loads", counting)
    return calls


def test_get_json_null_body_caches(monkeypatch):
    # A body that is JSON `null` parses to None. The cache must store that None
    # so a second access returns it without re-decoding; previously the `is None`
    # cache check conflated "not parsed yet" with "parsed to null" and re-parsed
    # on every call.
    calls = _count_loads(monkeypatch)
    req = _req(b"null")
    assert req.get_json() is None
    assert req.get_json() is None
    assert calls["n"] == 1


async def test_async_json_null_body_caches(monkeypatch):
    calls = _count_loads(monkeypatch)
    req = _req(b"null")
    assert await req.json() is None
    assert await req.json() is None
    assert calls["n"] == 1
