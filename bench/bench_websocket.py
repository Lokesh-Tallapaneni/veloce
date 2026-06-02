"""In-process WebSocket / SSE throughput + latency benchmark.

Drives Veloce's ASGI surface on a single event loop with no sockets and no
uvicorn: the harness plays the ASGI server role itself, feeding the app a
``websocket`` scope plus a controlled ``receive`` / ``send`` pair. That
isolates the framework's per-message cost (frame envelope handling, DI
resolver, JSON encode/decode) from kernel + asyncio socket overhead.

Two workloads:

  * WebSocket JSON-echo - the client queues N ``websocket.receive`` messages
    carrying JSON, the handler decodes + re-encodes each and sends it back.
    Reports frames/sec and per-message latency percentiles.
  * SSE throughput - drains an ``EventSourceResponse`` stream of N events
    through the same byte path ``stream_to`` / the ASGI emit would use.
    Reports events/sec.

This is the measurement tool the perf-claimed WebSocket features (WS-6 bulk
framing, WS-10 SSE streaming) are gated on. Heavy / persisted output goes
only under the gitignored ``internal/`` tree.

Run:  python bench/bench_websocket.py
      python bench/bench_websocket.py --messages 200000 --save
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import sys
import time
from pathlib import Path

# Run straight from a source checkout without an editable install.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from veloce import Veloce, WebSocket  # noqa: E402
from veloce._protocol_constants import (  # noqa: E402
    ASGI_EVENT_WS_ACCEPT,
    ASGI_EVENT_WS_CONNECT,
    ASGI_EVENT_WS_DISCONNECT,
    ASGI_EVENT_WS_RECEIVE,
    ASGI_EVENT_WS_SEND,
)
from veloce.sse import EventSourceResponse, ServerSentEvent  # noqa: E402

# A representative JSON-echo payload - small structured object, the shape a
# chat / presence / telemetry app pushes per frame.
_PAYLOAD = {"type": "msg", "id": 12345, "user": "alice", "body": "hello world"}
_PAYLOAD_TEXT = json.dumps(_PAYLOAD)


def _percentiles(samples_ns: list[int]) -> dict[str, float]:
    """Return p50/p90/p99 of a latency sample set, in microseconds."""
    ordered = sorted(samples_ns)
    n = len(ordered)

    def pick(q: float) -> float:
        idx = min(n - 1, int(q * n))
        return ordered[idx] / 1_000.0

    return {"p50_us": pick(0.50), "p90_us": pick(0.90), "p99_us": pick(0.99)}


class _WSDriver:
    """Plays the ASGI server side of a WebSocket connection in-process.

    ``receive`` hands the app a connect envelope, then ``messages`` JSON
    ``websocket.receive`` frames, then a single disconnect. ``send`` records
    a high-resolution timestamp for every ``websocket.send`` so the harness
    can derive per-message round-trip latency against the matching inbound
    send time.
    """

    def __init__(self, messages: int, latency: bool) -> None:
        self._remaining = messages
        self._latency = latency
        self._sent_in_at = 0
        # Monotonic timestamp of the most recent inbound frame handed to the
        # app, paired against the next outbound send to get round-trip ns.
        self._inflight_at = 0
        self.recv_count = 0
        self.send_count = 0
        self.samples_ns: list[int] = []

    async def receive(self) -> dict:
        if self._sent_in_at == 0:
            self._sent_in_at = 1
            return {"type": ASGI_EVENT_WS_CONNECT}
        if self._remaining > 0:
            self._remaining -= 1
            self.recv_count += 1
            if self._latency:
                self._inflight_at = time.perf_counter_ns()
            return {"type": ASGI_EVENT_WS_RECEIVE, "text": _PAYLOAD_TEXT}
        return {"type": ASGI_EVENT_WS_DISCONNECT, "code": 1000}

    async def send(self, message: dict) -> None:
        mtype = message["type"]
        if mtype == ASGI_EVENT_WS_SEND:
            self.send_count += 1
            if self._latency and self._inflight_at:
                self.samples_ns.append(time.perf_counter_ns() - self._inflight_at)
                self._inflight_at = 0
        elif mtype == ASGI_EVENT_WS_ACCEPT:
            pass


async def _build_ws_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def echo(ws: WebSocket) -> None:
        await ws.accept()
        async for data in ws.iter_json():
            await ws.send_json(data)

    # The TestClient init path runs the startup lifecycle; do the same so the
    # app is in the state a served app would be in.
    await app._run_lifecycle("startup")
    return app


async def _run_ws(app: Veloce, messages: int, latency: bool) -> _WSDriver:
    driver = _WSDriver(messages, latency)
    scope = {
        "type": "websocket",
        "path": "/ws",
        "query_string": b"",
        "headers": [(b"host", b"localhost")],
        "client": ("127.0.0.1", 12345),
    }
    await app(scope, driver.receive, driver.send)
    return driver


async def bench_websocket(messages: int, warmup: int) -> dict:
    app = await _build_ws_app()

    # Warm: exercise the dispatch + DI + framing path so codegen / caches are hot.
    await _run_ws(app, warmup, latency=False)

    gc.disable()
    t0 = time.perf_counter_ns()
    tp = await _run_ws(app, messages, latency=False)
    elapsed_ns = time.perf_counter_ns() - t0
    gc.enable()

    # A separate latency pass: timestamping every frame perturbs throughput,
    # so the two numbers come from two runs rather than one instrumented run.
    lat = await _run_ws(app, messages, latency=True)

    if tp.recv_count != messages or tp.send_count != messages:
        raise RuntimeError(
            f"echo mismatch: recv={tp.recv_count} send={tp.send_count} expected={messages}"
        )

    secs = elapsed_ns / 1_000_000_000
    return {
        "messages": messages,
        "elapsed_s": secs,
        "frames_per_s": messages / secs,
        "ns_per_msg": elapsed_ns / messages,
        **_percentiles(lat.samples_ns),
    }


class _SSESink:
    """Minimal transport capturing the byte stream ``stream_to`` emits."""

    __slots__ = ("byte_count", "chunk_count")

    def __init__(self) -> None:
        self.byte_count = 0
        self.chunk_count = 0

    def write(self, data: bytes) -> None:
        self.byte_count += len(data)
        self.chunk_count += 1

    def writelines(self, parts: tuple[bytes, ...]) -> None:
        for part in parts:
            self.byte_count += len(part)
        self.chunk_count += 1


async def _drain_sse(events: int) -> _SSESink:
    async def generate():
        payload = _PAYLOAD_TEXT
        for i in range(events):
            yield ServerSentEvent(data=payload, id=str(i), event="msg")

    response = EventSourceResponse(generate())
    sink = _SSESink()
    await response.stream_to(sink)
    return sink


async def bench_sse(events: int, warmup: int) -> dict:
    await _drain_sse(warmup)

    gc.disable()
    t0 = time.perf_counter_ns()
    sink = await _drain_sse(events)
    elapsed_ns = time.perf_counter_ns() - t0
    gc.enable()

    secs = elapsed_ns / 1_000_000_000
    return {
        "events": events,
        "elapsed_s": secs,
        "events_per_s": events / secs,
        "ns_per_event": elapsed_ns / events,
        "bytes_emitted": sink.byte_count,
    }


def _print_report(ws: dict, sse: dict) -> None:
    print("=" * 70)
    print("  VELOCE WebSocket / SSE - in-process benchmark")
    print("=" * 70)
    print("\n  WebSocket JSON-echo (ASGI surface, single loop, no sockets)")
    print(f"    messages          : {ws['messages']:>12,}")
    print(f"    elapsed           : {ws['elapsed_s']:>12.4f} s")
    print(f"    throughput        : {ws['frames_per_s']:>12,.0f} frames/s")
    print(f"    per-message       : {ws['ns_per_msg']:>12,.0f} ns/op")
    print(
        f"    latency p50/p90/p99: {ws['p50_us']:.2f} / {ws['p90_us']:.2f} / {ws['p99_us']:.2f} us"
    )
    print("\n  SSE EventSourceResponse throughput")
    print(f"    events            : {sse['events']:>12,}")
    print(f"    elapsed           : {sse['elapsed_s']:>12.4f} s")
    print(f"    throughput        : {sse['events_per_s']:>12,.0f} events/s")
    print(f"    per-event         : {sse['ns_per_event']:>12,.0f} ns/op")
    print(f"    bytes emitted     : {sse['bytes_emitted']:>12,}")
    print("\n" + "=" * 70)


def _save(result: dict) -> Path:
    # Persisted output lives only under the gitignored internal/ tree.
    out_dir = Path(__file__).resolve().parent.parent / "internal" / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"websocket_sse_{int(time.time())}.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return out_path


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--messages",
        type=int,
        default=100_000,
        help="WebSocket echo frames to drive (default: 100000)",
    )
    parser.add_argument(
        "--events", type=int, default=100_000, help="SSE events to stream (default: 100000)"
    )
    parser.add_argument(
        "--warmup", type=int, default=2_000, help="Warmup iterations per workload (default: 2000)"
    )
    parser.add_argument(
        "--save", action="store_true", help="Persist results JSON under internal/bench/"
    )
    args = parser.parse_args()

    ws = await bench_websocket(args.messages, args.warmup)
    sse = await bench_sse(args.events, args.warmup)
    _print_report(ws, sse)

    if args.save:
        path = _save({"websocket": ws, "sse": sse})
        print(f"  saved: {path}")


if __name__ == "__main__":
    asyncio.run(main())
