"""Request.get_data — body accessor."""

from __future__ import annotations

import inspect

from veloce import Request


def _req(body: bytes, content_type: str = "text/plain") -> Request:
    return Request(
        method="POST",
        path="/x",
        query_string="",
        headers={"content-type": content_type} if content_type else {},
        body=body,
    )


def test_request_get_data_is_coroutine_function():
    """`Request.get_data` must remain `async def`. Reverting it to a sync
    method would break the `await request.get_data()` idiom; this mirrors
    the `Request.json` / `Request.body` regression guards."""
    assert inspect.iscoroutinefunction(Request.get_data)


async def test_default_returns_raw_bytes():
    r = _req(b"hello")
    assert await r.get_data() == b"hello"
    assert isinstance(await r.get_data(), bytes)


async def test_as_text_default_utf8():
    r = _req("café".encode())
    assert await r.get_data(as_text=True) == "café"


async def test_as_text_uses_charset_from_content_type():
    r = _req("Ω".encode("utf-16"), content_type="text/plain; charset=utf-16")
    assert await r.get_data(as_text=True) == "Ω"


async def test_as_text_falls_back_to_latin1_on_bogus_charset():
    """Bogus charset must not raise — falls back to latin-1."""
    r = _req(b"\xff\xfe", content_type="text/plain; charset=not-a-real-encoding")
    # latin-1 round-trips any byte; we just need a non-raising decode.
    out = await r.get_data(as_text=True)
    assert isinstance(out, str)
    assert len(out) == 2


async def test_empty_body():
    r = _req(b"")
    assert await r.get_data() == b""
    assert await r.get_data(as_text=True) == ""


async def test_cache_param_accepted_but_noop():
    """`cache=False` must not raise today."""
    r = _req(b"x")
    assert await r.get_data(cache=False) == b"x"
