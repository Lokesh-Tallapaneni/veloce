"""Profile the in-loop dispatch path — same workload as hot_dispatch_bench."""

from __future__ import annotations

import asyncio
import cProfile
import pstats
from io import StringIO

from bench.hot_dispatch_bench import build_app, make_scope


async def amain() -> None:
    app = build_app()
    await app._run_lifecycle("startup")

    scope_index = make_scope("GET", "/")
    scope_item = make_scope("GET", "/items/42")

    async def run_one(s, body=b""):
        sent = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(msg):
            sent.append(msg)

        await app(s, receive, send)

    # Warm
    for _ in range(2000):
        await run_one(scope_index)
        await run_one(scope_item)

    pr = cProfile.Profile()
    pr.enable()
    for _ in range(8000):
        await run_one(scope_index)
    for _ in range(8000):
        await run_one(scope_item)
    pr.disable()

    s = StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("tottime")
    ps.print_stats(40)
    print(s.getvalue())


if __name__ == "__main__":
    asyncio.run(amain())
