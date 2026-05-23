"""Dispatch micro-benchmark for veloce.

Measures request-dispatch throughput for a few representative route
shapes through the in-memory `TestClient` (the same ASGI surface a real
server drives). Run it directly:

    python bench/dispatch_bench.py

It prints requests/second per shape. CI runs it on every push so a
throughput regression shows up in the build log.
"""

from __future__ import annotations

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


def _measure(label: str, call) -> None:
    for _ in range(WARMUP):
        call()
    start = time.perf_counter()
    for _ in range(ITERATIONS):
        call()
    elapsed = time.perf_counter() - start
    rate = ITERATIONS / elapsed
    print(f"  {label:28s} {rate:>10,.0f} req/s  ({elapsed / ITERATIONS * 1e6:6.1f} us)")


def main() -> None:
    client = TestClient(_build_app())
    print("veloce dispatch micro-benchmark")
    _measure("static GET", lambda: client.get("/"))
    _measure("path-param GET", lambda: client.get("/items/7"))
    _measure("POST 64-byte body", lambda: client.post("/echo", content=b"x" * 64))


if __name__ == "__main__":
    main()
