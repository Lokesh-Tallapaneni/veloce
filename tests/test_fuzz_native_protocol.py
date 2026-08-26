"""Feeds arbitrary and malformed bytes at the native `HttpProtocol` parser.

The fuzz suite covered six parsers - the router, cookies, headers, multipart,
signing and the websocket framer - and omitted the one that reads bytes straight
off a socket. `HttpProtocol.data_received` parses the request line, the header
block and the body framing of whatever an unauthenticated peer sends, before any
application code runs, and it is the transport `app.run()` and `VeloceWorker`
use. The recorded pattern in this project is that the serious defects cluster in
exactly that file.

The property under test is the one a parser on an untrusted boundary must hold:
**never raise out of `data_received`, and never hang.** A malformed request may
be answered with a `400`, or the connection dropped, or ignored - what it must
not do is escape as an unhandled exception (which kills the connection task and
logs a traceback for anything a peer chooses to send) or leave the parser stuck.

Bytes are also delivered in split runs, because a transport does not respect
frame boundaries: a request may arrive one byte at a time, and a parser that
only works on whole reads is broken in production and green in a test that
always feeds it complete requests.
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests._native_client import NativeTransport
from veloce import Veloce
from veloce.serving.protocol import HttpProtocol

CRLF = b"\r\n"


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    @app.post("/echo")
    async def echo(request):
        return {"n": len(await request.body())}

    return app


_APP = _app()


def _feed(chunks: list[bytes], *, turns: int = 40) -> NativeTransport:
    """Feed byte runs to a fresh connection; return the transport it wrote to.

    Any exception escaping `data_received` fails the test - that is the property
    under test, and it is why nothing here is wrapped in `try`.

    The loop is advanced `turns` times afterwards: `data_received` only *starts*
    the dispatch, so a single yield returns before anything has been written and
    an assertion on the response would see an empty transport.
    """
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_APP._run_lifecycle("startup"))
        protocol = HttpProtocol(_APP, loop)
        transport = NativeTransport()
        protocol.connection_made(transport)
        for chunk in chunks:
            if chunk:
                protocol.data_received(chunk)
        for _ in range(turns):
            loop.run_until_complete(asyncio.sleep(0))
        return transport
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.close()


_junk = st.binary(min_size=0, max_size=400)
_line_token = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=0, max_size=20
)

SETTINGS = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# ── arbitrary bytes ──────────────────────────────────────────────────


@SETTINGS
@given(payload=_junk)
def test_arbitrary_bytes_never_raise(payload: bytes) -> None:
    _feed([payload])


@SETTINGS
@given(payload=_junk)
def test_arbitrary_bytes_split_across_reads_never_raise(payload: bytes) -> None:
    """A transport does not respect message boundaries."""
    mid = len(payload) // 2
    _feed([payload[:mid], payload[mid:]])


@SETTINGS
@given(payload=st.binary(min_size=1, max_size=120))
def test_one_byte_at_a_time_never_raises(payload: bytes) -> None:
    """The narrowest split there is - a parser that needs whole reads fails here
    and nowhere else."""
    _feed([payload[i : i + 1] for i in range(len(payload))])


# ── malformed but request-shaped ─────────────────────────────────────


@SETTINGS
@given(method=_line_token, target=_line_token, version=_line_token)
def test_an_arbitrary_request_line_never_raises(method, target, version) -> None:
    line = f"{method} {target} {version}".encode("latin-1", "ignore")
    _feed([line + CRLF + CRLF])


@SETTINGS
@given(name=_line_token, value=_line_token)
def test_an_arbitrary_header_never_raises(name, value) -> None:
    header = f"{name}: {value}".encode("latin-1", "ignore")
    _feed([b"GET / HTTP/1.1" + CRLF + header + CRLF + CRLF])


@SETTINGS
@given(length=st.text(alphabet="0123456789-+eE. ", min_size=0, max_size=12))
def test_an_arbitrary_content_length_never_raises(length: str) -> None:
    """The declared length is attacker-controlled and reaches `int()`."""
    head = b"POST /echo HTTP/1.1" + CRLF + b"content-length: " + length.encode() + CRLF + CRLF
    _feed([head, b"body"])


@SETTINGS
@given(size=st.text(alphabet="0123456789abcdefABCDEFxX-", min_size=0, max_size=10))
def test_an_arbitrary_chunk_size_never_raises(size: str) -> None:
    head = b"POST /echo HTTP/1.1" + CRLF + b"transfer-encoding: chunked" + CRLF + CRLF
    _feed([head, size.encode() + CRLF + b"data" + CRLF + b"0" + CRLF + CRLF])


@SETTINGS
@given(count=st.integers(min_value=0, max_value=60))
def test_many_headers_never_raise(count: int) -> None:
    headers = b"".join(f"x-h{i}: v".encode() + CRLF for i in range(count))
    _feed([b"GET / HTTP/1.1" + CRLF + headers + CRLF])


@SETTINGS
@given(payload=_junk)
def test_junk_after_a_valid_request_never_raises(payload: bytes) -> None:
    """Pipelining means the parser must survive whatever follows a good request."""
    _feed([b"GET / HTTP/1.1" + CRLF + CRLF, payload])


# ── a well-formed request still works ────────────────────────────────
#
# The negative that matters: a parser that answered nothing at all would pass
# every property above.


def test_a_well_formed_request_is_answered():
    transport = _feed([b"GET / HTTP/1.1" + CRLF + b"host: x" + CRLF + CRLF])
    written = b"".join(transport.writes)
    assert written.startswith(b"HTTP/1.1 200"), written[:60]


def test_a_well_formed_request_split_one_byte_at_a_time_is_answered():
    request = b"GET / HTTP/1.1" + CRLF + b"host: x" + CRLF + CRLF
    transport = _feed([request[i : i + 1] for i in range(len(request))])
    assert b"".join(transport.writes).startswith(b"HTTP/1.1 200")


@pytest.mark.parametrize(
    "bad",
    [
        b"\x00\x01\x02" + CRLF + CRLF,
        b"GET" + CRLF + CRLF,
        b"GET / HTTP/9.9" + CRLF + CRLF,
        b"GET / HTTP/1.1" + CRLF + b"bad header no colon" + CRLF + CRLF,
        b"POST /echo HTTP/1.1" + CRLF + b"content-length: notanumber" + CRLF + CRLF,
    ],
    ids=["control-bytes", "truncated-line", "bad-version", "headerless-colon", "bad-length"],
)
def test_a_malformed_request_is_refused_rather_than_crashing(bad):
    """Refused, dropped or ignored are all acceptable; raising is not."""
    transport = _feed([bad])
    written = b"".join(transport.writes)
    if written:
        assert written.startswith(b"HTTP/1.1"), written[:60]


# ── the split that this module found ─────────────────────────────────
#
# Fuzzing this parser found a real defect on its first run, which is the case
# for adding it: `on_url` is an *incremental* callback, like `on_body`. When the
# request target spans two reads httptools delivers it in two calls - and the
# second is `b""` when the split falls immediately after the target. The
# callback assigned rather than appended, so the accumulated target was replaced
# by the tail:
#
#     data_received(b"GET /")
#     data_received(b" HTTP/1.1\r\nhost: x\r\n\r\n")   ->  400 Bad Request
#
# while the identical bytes in one read answered 200. A target split across a
# TCP segment boundary is ordinary traffic, not an attack.


def _target_split_at(index: int) -> bytes:
    request = b"GET /items/42 HTTP/1.1" + CRLF + b"host: x" + CRLF + CRLF
    return b"".join(_feed([request[:index], request[index:]]).writes)


def _echo_app_response(chunks: list[bytes]) -> bytes:
    return b"".join(_feed(chunks).writes)


def test_a_request_target_split_across_reads_is_served():
    """The defect: this answered 400."""
    request = b"GET / HTTP/1.1" + CRLF + b"host: x" + CRLF + CRLF
    written = b"".join(_feed([request[:5], request[5:]]).writes)
    assert written.startswith(b"HTTP/1.1 200"), written[:60]


@pytest.mark.parametrize("index", list(range(1, 27)))
def test_every_split_point_of_a_valid_request_is_served(index: int):
    """Stated exhaustively, because the failing split was a single index out of
    26 and any narrower test would have missed it."""
    request = b"GET / HTTP/1.1" + CRLF + b"host: x" + CRLF + CRLF
    written = b"".join(_feed([request[:index], request[index:]]).writes)
    assert written.startswith(b"HTTP/1.1 200"), (index, written[:60])


def test_a_longer_target_survives_every_split_point():
    request = b"GET /items/42 HTTP/1.1" + CRLF + b"host: x" + CRLF + CRLF
    for index in range(1, len(request)):
        written = _target_split_at(index)
        assert written.startswith(b"HTTP/1.1"), (index, written[:60])


def test_an_oversized_target_is_measured_across_reads():
    """The size limit now sees the accumulated target, not the last fragment.

    Splitting an over-limit URL used to leave a short tail in `self.url`, so the
    `414` became a `400` - the request was refused for the wrong reason, and a
    limit measured per-fragment is not measuring the limit.
    """
    from veloce.serving.protocol import MAX_URL_SIZE

    target = b"/" + b"a" * (MAX_URL_SIZE + 100)
    request = b"GET " + target + b" HTTP/1.1" + CRLF + b"host: x" + CRLF + CRLF
    half = 4 + len(target) // 2

    assert _echo_app_response([request]).startswith(b"HTTP/1.1 414")
    assert _echo_app_response([request[:half], request[half:]]).startswith(b"HTTP/1.1 414")


def test_a_target_under_the_limit_is_still_served_when_split():
    """The negative: accumulating must not make an ordinary URL look oversized."""
    from veloce.serving.protocol import MAX_URL_SIZE

    target = b"/" + b"a" * (MAX_URL_SIZE // 2)
    request = b"GET " + target + b" HTTP/1.1" + CRLF + b"host: x" + CRLF + CRLF
    half = 4 + len(target) // 2
    written = _echo_app_response([request[:half], request[half:]])
    assert not written.startswith(b"HTTP/1.1 414"), written[:60]


def test_two_pipelined_requests_do_not_share_a_target():
    """`self.url` accumulates, so it must be cleared between requests - or the
    second request's target would be appended to the first's."""
    request = b"GET / HTTP/1.1" + CRLF + b"host: x" + CRLF + CRLF
    written = b"".join(_feed([request, request], turns=120).writes)
    assert written.count(b"HTTP/1.1 200") == 2, written[:120]
