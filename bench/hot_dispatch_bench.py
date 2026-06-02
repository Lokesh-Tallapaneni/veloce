"""In-loop dispatch micro-benchmark.

Runs the ASGI app callable directly inside one running event loop, so
TestClient's per-call loop spin-up overhead does not dominate the timing.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any

from veloce import Veloce


def build_app() -> Veloce:
    app = Veloce(openapi_url=None)

    async def index():
        return {"ok": True}

    async def show(item_id: int):
        return {"id": item_id}

    async def echo(request):
        return {"len": len(await request.body())}

    app.add_route("/", index, ["GET"])
    app.add_route("/items/{item_id}", show, ["GET"])
    app.add_route("/echo", echo, ["POST"])
    return app


def make_scope(method: str, path: str, body: bytes = b"") -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1")],
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 12345),
    }


def make_runner(app, scope: dict[str, Any], body: bytes = b""):
    """Return an `async runner()` that drives one ASGI cycle."""

    async def runner():
        sent_messages: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(msg):
            sent_messages.append(msg)

        await app(scope, receive, send)
        return sent_messages

    return runner


async def measure(label: str, runner_factory, iters: int, warmup: int) -> tuple[str, float]:
    # Build fresh runners each call so we touch only dispatch, not state.
    for _ in range(warmup):
        await runner_factory()
    start = time.perf_counter()
    for _ in range(iters):
        await runner_factory()
    elapsed = time.perf_counter() - start
    return label, iters / elapsed


async def amain(iters: int, warmup: int) -> None:
    app = build_app()
    # Lifespan startup so first-request init doesn't taint timings.
    app._run_lifecycle_sync = None  # no-op
    await app._run_lifecycle("startup")

    cases = [
        ("static GET     ", make_scope("GET", "/"), b""),
        ("path-param GET ", make_scope("GET", "/items/42"), b""),
        ("POST 64 body   ", make_scope("POST", "/echo"), b"x" * 64),
    ]

    print("veloce in-loop dispatch bench")
    print(f"  iterations: {iters}, warmup: {warmup}")
    for label, scope, body in cases:

        async def factory(s=scope, b=body):
            sent: list = []

            async def receive():
                return {"type": "http.request", "body": b, "more_body": False}

            async def send(msg):
                sent.append(msg)

            await app(s, receive, send)

        label, rps = await measure(label, factory, iters, warmup)
        us = 1_000_000.0 / rps if rps else 0
        print(f"  {label:<18} {rps:>10,.0f} req/s   ({us:>6.2f} us)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=30_000)
    ap.add_argument("--warmup", type=int, default=3000)
    args = ap.parse_args()
    asyncio.run(amain(args.iters, args.warmup))


if __name__ == "__main__":
    main()
