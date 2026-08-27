"""A `MAX_CONTENT_LENGTH` refusal looks the same however the app is served.

The two transports answered the same condition two different ways:

    ASGI   413 application/json {"detail":"Request body exceeds MAX_CONTENT_LENGTH",
                                 "status_code":413,"limit":10}
    native 413 (no content type) b"Content Too Large" + Connection: close

So a client written against the documented error shape — the one the ASGI path
sends — got plain text with no content type from `app.run()`. Same app, same
request, same limit; the answer depended on which server was in front of it.

The payload is built in one place now (`http/_body.too_large_payload`) and both
transports send it. The native path keeps `Connection: close`, which is correct:
it stops reading a body it has already refused.

These tests drive `HttpProtocol` directly for the native half and read the reply
off the wire, because the test client only exercises the ASGI path — which is
exactly why the divergence went unnoticed.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

from tests._asgi_drive import body_of, drive, headers_of, status_of
from veloce import Veloce
from veloce.http._body import too_large_payload
from veloce.serving.protocol import HttpProtocol

LIMIT = 10
CRLF = b"\r\n"


def _app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = LIMIT

    @app.post("/up")
    async def up(request) -> dict:
        return {"got": len(await request.body())}

    return app


def _scope(body: bytes) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/up",
        "raw_path": b"/up",
        "query_string": b"",
        "headers": [(b"content-length", str(len(body)).encode())],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
        "scheme": "http",
        "root_path": "",
    }


async def _drive_asgi(body: bytes, app: Veloce | None = None) -> tuple[int, dict, bytes]:
    """Send one POST through the ASGI entry point."""
    messages = await drive(app or _app(), _scope(body), body=body)
    return status_of(messages), headers_of(messages), body_of(messages)


class _FakeTransport(asyncio.Transport):
    """Captures what the native protocol writes, as the rest of the suite does."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name, default=None):
        return default


def _drive_native_raw(body: bytes) -> tuple[bytes, bool]:
    """Drive one raw POST through `HttpProtocol`; return the wire bytes and close flag.

    Split from `_drive_native` because the parsed form gives back a status
    *code*, and a test asserting on the reason phrase needs the status line.
    Without this the phrase test re-inlined the whole drive.
    """
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(_app(), loop)
        transport = _FakeTransport()
        proto.connection_made(transport)
        head = b"POST /up HTTP/1.1" + CRLF + b"Host: t" + CRLF
        head += b"Content-Length: " + str(len(body)).encode() + CRLF + CRLF
        proto.data_received(head + body)
        for _ in range(4):
            loop.run_until_complete(asyncio.sleep(0))
        return b"".join(transport.writes), transport.closed
    finally:
        loop.close()


def _drive_native(body: bytes) -> tuple[int, dict, bytes, bool]:
    """Drive one raw POST through `HttpProtocol` and parse the reply off the wire."""
    emitted, closed = _drive_native_raw(body)

    head_bytes, _, payload = emitted.partition(CRLF + CRLF)
    lines = head_bytes.split(CRLF)
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        if name:
            headers[name.decode().strip().lower()] = value.decode().strip()
    return status, headers, payload, closed


# ── the shared payload ───────────────────────────────────────────────


def test_the_payload_names_the_limit():
    assert too_large_payload(LIMIT) == {
        "detail": "Request body exceeds MAX_CONTENT_LENGTH",
        "status_code": 413,
        "limit": LIMIT,
    }


def test_an_unset_limit_is_carried_as_none():
    """The field is always present, so a client need not branch on its absence."""
    assert too_large_payload(None)["limit"] is None


def test_the_builder_has_one_definition():
    """Two copies is how the two transports came to disagree."""
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce"
    dispatch = (root / "app" / "dispatch.py").read_text(encoding="utf-8")
    protocol = (root / "serving" / "protocol.py").read_text(encoding="utf-8")
    assert "too_large_payload(" in dispatch
    assert "too_large_payload(" in protocol
    assert '"detail": MSG_REQUEST_BODY_EXCEEDS_MAX' not in dispatch


# ── the ASGI path is unchanged ───────────────────────────────────────


async def test_asgi_refuses_an_oversized_body():
    status, headers, body = await _drive_asgi(b"x" * 100)
    assert status == 413
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body) == too_large_payload(LIMIT)


async def test_asgi_accepts_a_body_at_the_limit():
    """The negative case for the refusal: exactly at the limit is allowed."""
    status, _headers, body = await _drive_asgi(b"x" * LIMIT)
    assert status == 200
    assert json.loads(body) == {"got": LIMIT}


async def test_asgi_accepts_a_body_under_the_limit():
    status, _headers, body = await _drive_asgi(b"x")
    assert status == 200
    assert json.loads(body) == {"got": 1}


async def test_asgi_accepts_an_empty_body():
    status, _headers, _body = await _drive_asgi(b"")
    assert status == 200


# ── the native path now matches ──────────────────────────────────────


def test_native_refuses_an_oversized_body():
    """The defect: this was `b"Content Too Large"` with no content type."""
    status, headers, body, _closed = _drive_native(b"x" * 100)
    assert status == 413
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body) == too_large_payload(LIMIT)


def test_native_sends_a_correct_content_length():
    """The framing has to match the new body or the connection is corrupt."""
    _status, headers, body, _closed = _drive_native(b"x" * 100)
    assert int(headers["content-length"]) == len(body)


def test_native_still_closes_the_connection():
    """It has refused the body; continuing to read it would be the bug."""
    _status, headers, _body, closed = _drive_native(b"x" * 100)
    assert headers["connection"].lower() == "close"
    assert closed is True


def test_native_keeps_the_reason_phrase():
    """The status *line*, which the parsed form reduces to a code."""
    emitted, _closed = _drive_native_raw(b"x" * 100)
    assert b"413 Content Too Large" in emitted


def test_native_accepts_a_body_at_the_limit():
    status, _headers, body, _closed = _drive_native(b"x" * LIMIT)
    assert status == 200
    assert json.loads(body) == {"got": LIMIT}


def test_native_accepts_a_body_under_the_limit():
    status, _headers, body, _closed = _drive_native(b"x")
    assert status == 200
    assert json.loads(body) == {"got": 1}


def _asgi_sync(body: bytes) -> tuple[int, dict, bytes]:
    """The ASGI half on its own loop, so one test can drive both transports."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_drive_asgi(body))
    finally:
        loop.close()


# ── the two agree ────────────────────────────────────────────────────


def test_both_transports_send_the_same_refusal():
    """The property the fix is for, stated directly."""
    asgi_status, asgi_headers, asgi_body = _asgi_sync(b"x" * 100)
    native_status, native_headers, native_body, _closed = _drive_native(b"x" * 100)

    assert asgi_status == native_status == 413
    assert asgi_headers["content-type"].split(";")[0] == native_headers["content-type"]
    assert json.loads(asgi_body) == json.loads(native_body)


def test_both_transports_agree_on_an_accepted_body():
    asgi_status, _ah, asgi_body = _asgi_sync(b"x" * LIMIT)
    native_status, _nh, native_body, _closed = _drive_native(b"x" * LIMIT)
    assert asgi_status == native_status == 200
    assert json.loads(asgi_body) == json.loads(native_body)


def test_the_refusal_body_is_parseable_json_on_both():
    """A client parses it; both must be JSON, not one text and one JSON."""
    _s, _h, asgi_body = _asgi_sync(b"x" * 100)
    _ns, _nh, native_body, _c = _drive_native(b"x" * 100)
    for body in (asgi_body, native_body):
        parsed = json.loads(body)
        assert parsed["status_code"] == 413
        assert parsed["limit"] == LIMIT


# ── no limit configured ──────────────────────────────────────────────


def _unlimited_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.post("/up")
    async def up(request) -> dict:
        return {"got": len(await request.body())}

    return app


async def test_no_limit_accepts_a_large_body():
    """Negative case for the whole feature: nothing is refused when unset."""
    status, _headers, body = await _drive_asgi(b"x" * 10_000, app=_unlimited_app())
    assert status == 200
    assert json.loads(body) == {"got": 10_000}
