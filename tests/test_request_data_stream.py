"""Request.body + Request.data + Request.text + Request.stream — body accessors."""

from __future__ import annotations

import inspect

import pytest

from tests.conftest import make_request
from veloce import Request


def _req(body: bytes = b"") -> Request:
    return make_request(method="POST", path="/", query_string="", headers={}, body=body)


# ── Request.body (async) ────────────────────────────────────────────


def test_request_body_is_coroutine_function():
    """`Request.body` must remain `async def`. Reverting it to a sync
    attribute/property would silently break the `await request.body()`
    idiom; this mirrors the `Request.json` regression guard."""
    assert inspect.iscoroutinefunction(Request.body)


async def test_body_awaits_to_raw_bytes():
    assert await _req(b"payload").body() == b"payload"


async def test_body_empty_body():
    assert await _req(b"").body() == b""


# ── Request.text (async) ────────────────────────────────────────────


def test_request_text_is_coroutine_function():
    """`Request.text` must remain `async def` — same guard rationale as
    `body()`."""
    assert inspect.iscoroutinefunction(Request.text)


async def test_text_decodes_body():
    assert await _req(b"hello").text() == "hello"


# ── Request.data ────────────────────────────────────────────────────


def test_data_returns_raw_body():
    req = _req(b'{"x": 1}')
    assert req.data == b'{"x": 1}'


def test_data_empty_body():
    assert _req().data == b""


def test_data_is_bytes():
    assert isinstance(_req(b"hello").data, bytes)


def test_data_raises_when_body_not_yet_buffered():
    """The sync `request.data` accessor must refuse to return until the
    body has been drained — it points the caller at `await request.body()`.
    Pins the contract before Pass 2 makes the un-drained branch reachable."""
    req = _req(b"payload")
    req._body_drained = False
    with pytest.raises(RuntimeError, match=r"await request\.body\(\)"):
        _ = req.data


# ── Request.stream ──────────────────────────────────────────────────


async def test_stream_yields_whole_body_one_chunk():
    req = _req(b"chunk-of-data")
    chunks = [c async for c in req.stream()]
    assert chunks == [b"chunk-of-data"]


async def test_stream_empty_body_yields_nothing():
    req = _req()
    chunks = [c async for c in req.stream()]
    assert chunks == []


async def test_stream_reassembles_to_full_body():
    req = _req(b"the complete payload")
    assembled = b"".join([c async for c in req.stream()])
    assert assembled == await req.body()
