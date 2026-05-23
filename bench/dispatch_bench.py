"""Dispatch micro-benchmark for veloce.

Measures request-dispatch throughput for a few representative route
shapes through the in-memory `TestClient` (the same ASGI surface a real
server drives). Run it directly:

    python bench/dispatch_bench.py
    python bench/dispatch_bench.py --min-rps 2000

It prints requests/second per shape. With `--min-rps` it exits non-zero
if any shape falls below the floor — CI passes a deliberately loose
value so only a catastrophic (multi-x) regression fails the build;
runner-to-runner variance stays well clear of it.
"""

from __future__ import annotations

import argparse
import sys
import time

from veloce import Request, Veloce
from veloce.testclient import TestClient

WARMUP = 400
ITERATIONS = 12_000


def _build_app() -> Veloce:
    app = Veloce(openapi_url=None)

    async def index():
        return {"ok": True}

    async def show(item_id: int):
        return {"id": item_id}

    async def echo(request: Request):
        return {"len": len(request.body)}

    app.add_route("/", index, ["GET"])
    app.add_route("/items/{item_id}", show, ["GET"])
    app.add_route("/echo", echo, ["POST"])
    return app


def _measure(label: str, call) -> float:
    for _ in range(WARMUP):
        call()
    start = time.perf_counter()
    for _ in range(ITERATIONS):
        call()
    elapsed = time.perf_counter() - start
    rate = ITERATIONS / elapsed
    print(f"  {label:28s} {rate:>10,.0f} req/s  ({elapsed / ITERATIONS * 1e6:6.1f} us)")
    return rate


def main() -> int:
    parser = argparse.ArgumentParser(description="veloce dispatch micro-benchmark")
    parser.add_argument(
        "--min-rps",
        type=float,
        default=None,
        help="Fail (exit 1) if any shape falls below this requests/second floor.",
    )
    args = parser.parse_args()

    client = TestClient(_build_app())
    print("veloce dispatch micro-benchmark")
    rates = [
        _measure("static GET", lambda: client.get("/")),
        _measure("path-param GET", lambda: client.get("/items/7")),
        _measure("POST 64-byte body", lambda: client.post("/echo", content=b"x" * 64)),
    ]

    if args.min_rps is not None and min(rates) < args.min_rps:
        print(
            f"\nFAIL: slowest shape {min(rates):,.0f} req/s is below the "
            f"{args.min_rps:,.0f} req/s floor — investigate a dispatch regression."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
