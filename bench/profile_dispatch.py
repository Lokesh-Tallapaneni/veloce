"""Profile a single request through TestClient — find hot lines."""

from __future__ import annotations

import cProfile
import pstats
from io import StringIO

from veloce import Veloce
from veloce.testclient import TestClient


def _build():
    app = Veloce(openapi_url=None)

    async def index():
        return {"ok": True}

    async def show(item_id: int):
        return {"id": item_id}

    app.add_route("/", index, ["GET"])
    app.add_route("/items/{item_id}", show, ["GET"])
    return app


def main() -> None:
    app = _build()
    client = TestClient(app)

    # Warm up
    for _ in range(400):
        client.get("/")
        client.get("/items/42")

    pr = cProfile.Profile()
    pr.enable()
    for _ in range(4000):
        client.get("/")
    for _ in range(4000):
        client.get("/items/42")
    pr.disable()

    s = StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
    ps.print_stats(40)
    print(s.getvalue())


if __name__ == "__main__":
    main()
