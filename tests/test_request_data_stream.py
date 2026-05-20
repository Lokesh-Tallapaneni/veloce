"""Request.data + Request.stream — request body accessors."""

from __future__ import annotations

import pytest

from veloce import Request


def _req(body: bytes = b"") -> Request:
    return Request(method="POST", path="/", query_string="", headers={}, body=body)


# ── Request.data ────────────────────────────────────────────────────


def test_data_returns_raw_body():
    req = _req(b'{"x": 1}')
    assert req.data == b'{"x": 1}'


def test_data_empty_body():
    assert _req().data == b""


def test_data_is_bytes():
    assert isinstance(_req(b"hello").data, bytes)


# ── Request.stream ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_yields_whole_body_one_chunk():
    req = _req(b"chunk-of-data")
    chunks = [c async for c in req.stream()]
    assert chunks == [b"chunk-of-data"]


@pytest.mark.asyncio
async def test_stream_empty_body_yields_nothing():
    req = _req()
    chunks = [c async for c in req.stream()]
    assert chunks == []


@pytest.mark.asyncio
async def test_stream_reassembles_to_full_body():
    req = _req(b"the complete payload")
    assembled = b"".join([c async for c in req.stream()])
    assert assembled == req.body
