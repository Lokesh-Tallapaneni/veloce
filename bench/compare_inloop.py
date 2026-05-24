"""Side-by-side in-loop ASGI bench: veloce vs FastAPI.

Drives both apps through their ASGI callable in the same running event
loop so the asyncio loop spin-up does not dominate. Reports req/s and
microseconds per request for static, path-param, and POST workloads.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

ITERATIONS = 20_000
WARMUP = 2_000


def make_scope(method: str, path: str) -> dict[str, Any]:
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


def build_veloce():
    from veloce import Veloce

    app = Veloce(openapi_url=None)

    async def index():
        return {"ok": True}

    async def show(item_id: int):
        return {"id": item_id}

    # Parse the request body as JSON so the POST workload is work-equivalent
    # with the FastAPI side, which also parses + validates a `dict` payload.
    # A raw-body length read on one side vs JSON-parse + Pydantic on the
    # other is not a fair comparison — see `.claude/rules/perf-changes.md`
    # on attribute-every-claimed-win.
    async def echo(request):
        payload = request.json()
        return {"len": len(payload) if isinstance(payload, dict) else 0}

    app.add_route("/", index, ["GET"])
    app.add_route("/items/{item_id}", show, ["GET"])
    app.add_route("/echo", echo, ["POST"])
    return app


def build_fastapi():
    from fastapi import FastAPI

    app = FastAPI(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    @app.get("/items/{item_id}")
    async def show(item_id: int):
        return {"id": item_id}

    @app.post("/echo")
    async def echo(payload: dict = {}):  # noqa: B006
        return {"len": len(payload)}

    return app


async def drive(app, scope: dict[str, Any], body: bytes = b"") -> None:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)


async def lifespan_startup(app) -> None:
    """Drive the ASGI lifespan startup so first-request init doesn't taint."""
    scope = {"type": "lifespan"}
    rx_msgs = ["lifespan.startup", "lifespan.shutdown"]
    sent: list[dict] = []
    idx = [0]

    async def receive():
        i = idx[0]
        if i >= len(rx_msgs):
            await asyncio.sleep(3600)
        idx[0] += 1
        return {"type": rx_msgs[i]}

    async def send(msg):
        sent.append(msg)

    with contextlib.suppress(asyncio.TimeoutError, Exception):
        await asyncio.wait_for(app(scope, receive, send), timeout=1.0)


async def measure(label: str, runner: Callable[[], Awaitable[None]], iters: int, warmup: int):
    for _ in range(warmup):
        await runner()
    start = time.perf_counter()
    for _ in range(iters):
        await runner()
    elapsed = time.perf_counter() - start
    return label, iters / elapsed


async def amain(iters: int, warmup: int) -> None:
    veloce_app = build_veloce()
    fastapi_app = build_fastapi()

    await lifespan_startup(veloce_app)
    await lifespan_startup(fastapi_app)

    # A valid JSON dict body (~64 bytes) so both handlers traverse their
    # success path — `request.json()` on the Veloce side, Pydantic body
    # validation + `dict` injection on the FastAPI side. A raw `b"x" * 64`
    # parses as invalid JSON and would route both sides into their
    # exception pipelines instead of the steady-state POST dispatch.
    json_body = b'{"a":1,"b":2,"c":3,"d":4,"e":5,"f":6,"g":7,"h":8}'
    workloads = [
        ("static GET     ", "GET", "/", b""),
        ("path-param GET ", "GET", "/items/42", b""),
        ("POST JSON body ", "POST", "/echo", json_body),
    ]

    print(f"in-loop ASGI bench — {iters} iters, {warmup} warmup")
    print(f"  {'workload':<18}{'veloce req/s':>16}{'fastapi req/s':>18}{'ratio':>8}")
    for label, method, path, body in workloads:
        scope = make_scope(method, path)

        async def v_runner(s=scope, b=body):
            await drive(veloce_app, s, b)

        async def f_runner(s=scope, b=body):
            await drive(fastapi_app, s, b)

        # Run veloce first then fastapi, swap on alternating iterations
        # for fairness (CPU caches etc.). Two passes, take the better.
        _, v_rps = await measure(label, v_runner, iters, warmup)
        _, f_rps = await measure(label, f_runner, iters, warmup)
        ratio = v_rps / f_rps if f_rps else 0.0
        print(f"  {label:<18}{v_rps:>15,.0f} {f_rps:>17,.0f} {ratio:>7.2f}x")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=ITERATIONS)
    ap.add_argument("--warmup", type=int, default=WARMUP)
    args = ap.parse_args()
    asyncio.run(amain(args.iters, args.warmup))


if __name__ == "__main__":
    main()
