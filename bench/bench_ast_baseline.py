"""Independent dispatch baseline — perf_counter, single loop, in-process.

Measures handle_request ns/op for three route shapes that bracket the
AST-compilation audit's targets, plus standalone cost of the components the
audit wants to compile (router.match, resolver.resolve_plan,
_apply_response_model). perf_counter (not cProfile) for accurate absolutes;
median of B batches after warmup to damp scheduler noise.

Usage: python bench/bench_ast_baseline.py [--iters N] [--batches B] [--warmup W]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time

sys.path.insert(0, "src")

from pydantic import BaseModel  # noqa: E402

from veloce import Depends, Veloce  # noqa: E402
from veloce.app import _current_app_var  # noqa: E402
from veloce.http.request import Request  # noqa: E402


class Item(BaseModel):
    id: int
    name: str
    score: float


def _get_db() -> dict[str, str]:
    return {"conn": "ok"}


def build_apps() -> dict[str, tuple[Veloce, "callable"]]:
    apps: dict[str, tuple[Veloce, callable]] = {}

    # (a) trivial request-only handler
    a = Veloce(openapi_url=None)

    async def index(request):  # noqa: ANN001
        return {"ok": True}

    a.add_route("/", index, ["GET"])
    apps["trivial GET /"] = (a, lambda: Request(method="GET", path="/", query_string="", headers=[(b"host", b"x")], body=b""))

    # (b) typed path param, no DI, no response_model
    b = Veloce(openapi_url=None)

    async def show(user_id: int):
        return {"id": user_id}

    b.add_route("/users/{user_id:int}", show, ["GET"])
    apps["path-param /users/{id:int}"] = (b, lambda: Request(method="GET", path="/users/42", query_string="", headers=[(b"host", b"x")], body=b""))

    # (b2) multi-param (2 path + 2 query) — where param-resolver codegen helps most
    b2 = Veloce(openapi_url=None)

    async def multi(x: int, y: int, a: str = "a", b: int = 0):
        return {"x": x, "y": y, "a": a, "b": b}

    b2.add_route("/m/{x:int}/{y:int}", multi, ["GET"])
    apps["multi-param 2path+2query"] = (b2, lambda: Request(method="GET", path="/m/3/4", query_string="a=hi&b=7", headers=[(b"host", b"x")], body=b""))

    # (c) path param + query + Depends + Pydantic response_model (AST #1-#4 maximal)
    c = Veloce(openapi_url=None)

    async def get_item(user_id: int, q: str = "x", db: dict = Depends(_get_db)) -> Item:
        return {"id": user_id, "name": q, "score": 1.5}

    c.add_route("/items/{user_id:int}", get_item, ["GET"], response_model=Item)
    apps["DI+resp_model /items/{id:int}"] = (c, lambda: Request(method="GET", path="/items/42", query_string="q=hello", headers=[(b"host", b"x")], body=b""))

    return apps


async def _drive(app: Veloce, make_req, n: int) -> None:
    for _ in range(n):
        await app.handle_request(make_req())


def bench(app: Veloce, make_req, iters: int, batches: int, warmup: int) -> tuple[float, float]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _current_app_var.set(app)
    loop.run_until_complete(_drive(app, make_req, warmup))
    samples = []
    for _ in range(batches):
        t0 = time.perf_counter()
        loop.run_until_complete(_drive(app, make_req, iters))
        samples.append((time.perf_counter() - t0) / iters * 1e9)
    loop.close()
    return statistics.median(samples), statistics.stdev(samples) if len(samples) > 1 else 0.0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--iters", type=int, default=20000)
    p.add_argument("--batches", type=int, default=9)
    p.add_argument("--warmup", type=int, default=5000)
    args = p.parse_args()

    print(f"perf_counter · iters={args.iters} batches={args.batches} warmup={args.warmup} · CPython {sys.version.split()[0]}")
    print(f"{'route shape':36s} {'median ns/op':>14s} {'stdev':>8s}")
    print("-" * 62)
    for name, (app, make_req) in build_apps().items():
        med, sd = bench(app, make_req, args.iters, args.batches, args.warmup)
        print(f"{name:36s} {med:14.0f} {sd:8.0f}")


if __name__ == "__main__":
    main()
