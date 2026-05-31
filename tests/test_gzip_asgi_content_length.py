"""GZipMiddleware must not emit a duplicate Content-Length on the ASGI wire.

The buffered ASGI emit path used to always prepend a framework-default
``content-length`` tuple and then re-emit every response header, so a
middleware-set ``Content-Length`` (e.g. the compressed length from
``GZipMiddleware``) appeared twice. curl tolerates that; strict HTTP clients
reject the response. The single emitted ``Content-Length`` must equal the
compressed body length, and ``Content-Encoding: gzip`` must survive.
"""

from __future__ import annotations

import gzip

from veloce import GZipMiddleware, Request, Veloce


async def _drive(app: Veloce, headers: list[tuple[bytes, bytes]]):
    """Run one GET / through the ASGI app and capture the emitted messages."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


def _start_headers(messages: list[dict]) -> list[tuple[bytes, bytes]]:
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["headers"]


def _body(messages: list[dict]) -> bytes:
    return next(m for m in messages if m["type"] == "http.response.body")["body"]


def _make_app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=0))

    @app.get("/")
    async def root(request: Request):
        return {"value": "x" * 5000}

    return app


async def test_gzip_response_emits_single_content_length():
    app = _make_app()
    messages = await _drive(app, [(b"accept-encoding", b"gzip")])

    headers = _start_headers(messages)
    keys = [k.lower() for k, _ in headers]

    # Exactly one content-length, no duplicate keys at all.
    assert keys.count(b"content-length") == 1
    assert len(keys) == len(set(keys))

    # content-encoding: gzip survives.
    assert (b"content-encoding", b"gzip") in [(k.lower(), v) for k, v in headers]

    # The single content-length matches the compressed body length.
    body = _body(messages)
    cl = next(v for k, v in headers if k.lower() == b"content-length")
    assert int(cl) == len(body)
    # Body really is gzip and decodes back to the original JSON payload.
    assert b"x" * 5000 in gzip.decompress(body)


async def test_non_gzip_common_case_unchanged():
    app = _make_app()
    # No Accept-Encoding -> no compression, framework defaults apply.
    messages = await _drive(app, [])

    headers = _start_headers(messages)
    keys = [k.lower() for k, _ in headers]

    assert keys.count(b"content-length") == 1
    assert keys.count(b"content-type") == 1
    assert b"content-encoding" not in keys

    body = _body(messages)
    cl = next(v for k, v in headers if k.lower() == b"content-length")
    assert int(cl) == len(body)
