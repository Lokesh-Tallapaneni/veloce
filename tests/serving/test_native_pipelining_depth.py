"""The native transport bounds how many pipelined requests it will queue.

`HttpProtocol` wires transport flow control to the request *body* source: when a
body buffer fills, the source calls `_pause_reading` and the socket stops being
read. A pipelined bodiless `GET` has no body, so that path never fires and the
queue had no other bound. Feeding pipelined `GET / HTTP/1.1\\r\\n\\r\\n` queued one
`Request`, one `RequestBodySource` and one `RouteMatch` per 27 bytes on the wire,
for as long as the peer kept writing:

    proto.data_received(b"GET / HTTP/1.1\\r\\nHost: t\\r\\n\\r\\n" * 50000)
    len(proto._request_queue)   # 50000
    transport.paused            # 0

Depth is now a second reason to pause reading, alongside a full body buffer. The
two are reference-counted: whichever reason paused, reading resumes only once
*both* are satisfied, so a resume from one cannot cancel the other's pause.

What this bounds, precisely: the connection stops pulling from the socket once
the queue reaches the limit. Bytes already delivered are still parsed - httptools
consumes a whole buffer and does not report a partial feed - so the residual is
the limit plus whatever one transport read held. That turns unbounded growth into
a bound of roughly one read buffer, which is what the body-source flow control
achieves for the case it covers.
"""

from __future__ import annotations

import asyncio

import pytest

from tests._loops import protocol_loop
from veloce import Veloce
from veloce.serving.protocol import HttpProtocol

_REQ = b"GET / HTTP/1.1\r\nHost: t\r\n\r\n"


class _Transport(asyncio.Transport):
    """Counts pause/resume alongside what the protocol writes."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.paused = 0
        self.resumed = 0
        self.reading = True
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        self.paused += 1
        self.reading = False

    def resume_reading(self) -> None:
        self.resumed += 1
        self.reading = True

    def get_extra_info(self, name, default=None):
        return default


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    return app


@pytest.fixture
def loop():
    with protocol_loop() as made:
        yield made


def _connect(loop, **config):
    app = _app()
    app.config.update(config)
    proto = HttpProtocol(app, loop)
    transport = _Transport()
    proto.connection_made(transport)
    return proto, transport


def _settle(loop, turns=6):
    for _ in range(turns):
        loop.run_until_complete(asyncio.sleep(0))


# ── the bound ────────────────────────────────────────────────────────


def test_a_pipelining_flood_pauses_reading(loop):
    """The defect: 50,000 queued and `pause_reading` never called."""
    proto, transport = _connect(loop, MAX_PIPELINED_REQUESTS=16)
    proto.data_received(_REQ * 5000)
    assert transport.paused >= 1


def test_the_queue_does_not_grow_without_bound_across_reads(loop):
    """The realistic shape: many transport reads, not one giant write.

    Once reading is paused the server stops asking for more, so a peer that
    keeps writing cannot keep growing the queue.
    """
    proto, transport = _connect(loop, MAX_PIPELINED_REQUESTS=16)
    for _ in range(20):
        if not transport.reading:
            break
        proto.data_received(_REQ * 8)
    assert not transport.reading
    assert len(proto._request_queue) <= 16 + 8


def test_reading_resumes_once_the_queue_drains(loop):
    proto, transport = _connect(loop, MAX_PIPELINED_REQUESTS=16)
    proto.data_received(_REQ * 64)
    assert transport.paused >= 1
    _settle(loop, turns=400)
    assert transport.resumed >= 1


def test_every_flooded_request_is_still_answered(loop):
    """The bound must throttle, never drop: 64 requests, 64 responses."""
    proto, transport = _connect(loop, MAX_PIPELINED_REQUESTS=16)
    proto.data_received(_REQ * 64)
    _settle(loop, turns=600)
    assert b"".join(transport.writes).count(b"HTTP/1.1 200") == 64


def test_the_limit_is_configurable(loop):
    proto, transport = _connect(loop, MAX_PIPELINED_REQUESTS=4)
    for _ in range(20):
        if not transport.reading:
            break
        proto.data_received(_REQ * 2)
    assert not transport.reading
    assert len(proto._request_queue) <= 4 + 2


def test_the_default_limit_is_a_positive_int():
    assert isinstance(HttpProtocol.MAX_PIPELINED_REQUESTS, int)
    assert HttpProtocol.MAX_PIPELINED_REQUESTS > 0


# ── ordinary pipelining is untouched ─────────────────────────────────
#
# The negatives. A depth bound that throttled normal keep-alive traffic would
# cost far more than the flood it prevents.


def test_a_single_request_never_pauses(loop):
    proto, transport = _connect(loop)
    proto.data_received(_REQ)
    _settle(loop)
    assert transport.paused == 0
    assert b"HTTP/1.1 200" in b"".join(transport.writes)


def test_a_short_pipeline_never_pauses(loop):
    """Well under the default limit - the common legitimate case."""
    proto, transport = _connect(loop)
    proto.data_received(_REQ * 8)
    _settle(loop, turns=200)
    assert transport.paused == 0


def test_a_short_pipeline_answers_every_request_in_order(loop):
    proto, transport = _connect(loop)
    proto.data_received(_REQ * 8)
    _settle(loop, turns=200)
    assert b"".join(transport.writes).count(b"HTTP/1.1 200") == 8


def test_sequential_keep_alive_requests_never_pause(loop):
    """Depth counts what is queued, not what has been served."""
    proto, transport = _connect(loop, MAX_PIPELINED_REQUESTS=4)
    for _ in range(30):
        proto.data_received(_REQ)
        _settle(loop, turns=30)
    assert transport.paused == 0


# ── the two pause reasons do not cancel each other ───────────────────


def test_a_body_pause_is_not_undone_by_a_depth_resume(loop):
    """Reference-counted: reading resumes only when both reasons are clear."""
    proto, transport = _connect(loop, MAX_PIPELINED_REQUESTS=4)
    proto._pause_reading()  # the body source's reason
    assert not transport.reading
    proto._resume_reading_depth()
    assert not transport.reading, "a depth resume cleared the body source's pause"
    proto._resume_reading()
    assert transport.reading


def test_a_depth_pause_is_not_undone_by_a_body_resume(loop):
    proto, transport = _connect(loop, MAX_PIPELINED_REQUESTS=4)
    proto._pause_reading_depth()
    assert not transport.reading
    proto._resume_reading()
    assert not transport.reading, "a body resume cleared the depth pause"
    proto._resume_reading_depth()
    assert transport.reading


def test_both_reasons_at_once_pause_the_transport_once(loop):
    """asyncio pairs pause/resume; a double pause must not double-count."""
    proto, transport = _connect(loop, MAX_PIPELINED_REQUESTS=4)
    proto._pause_reading()
    proto._pause_reading_depth()
    assert transport.paused == 1
    proto._resume_reading()
    proto._resume_reading_depth()
    assert transport.resumed == 1


def test_a_repeated_pause_for_one_reason_is_idempotent(loop):
    proto, transport = _connect(loop, MAX_PIPELINED_REQUESTS=4)
    proto._pause_reading()
    proto._pause_reading()
    proto._resume_reading()
    assert transport.paused == 1
    assert transport.resumed == 1
    assert transport.reading


def test_a_resume_without_a_pause_does_nothing(loop):
    proto, transport = _connect(loop)
    proto._resume_reading()
    proto._resume_reading_depth()
    assert transport.resumed == 0


def test_pausing_a_closed_transport_is_safe(loop):
    """The existing guard: teardown must not raise out of a flow-control call."""
    proto, transport = _connect(loop)
    transport.closed = True
    proto._pause_reading_depth()
    proto._resume_reading_depth()
    assert transport.paused == 0
