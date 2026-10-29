"""One app served by more than one event loop in the same process.

A thread-per-core server, and any worker thread that drives a shared app with
its own loop, put several event loops over one `Veloce` instance. Both lazy
first-request latches were written as if only one loop could ever reach them:
the `before_first_request` latch held an `asyncio.Lock`, which binds to the
first loop that blocks on it and raises for every later loop, and
`_setup_openapi` advertised itself as done before the routes it registers
existed.

The tests drive the ASGI interface directly rather than over a socket - the
latches are reached the same way, and no port is needed. Every request is
bounded by `wait_for` so a regression reports instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from veloce import Veloce
from veloce.contrib import docs_ui

#: Long enough that every thread reaches the latch while the winner holds it.
HOOK_SECONDS = 0.2
#: A regression must fail, not hang. Generous against the hook above.
REQUEST_TIMEOUT = 5.0


def _scope(path: str = "/health") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


async def _request(app: Veloce, path: str = "/health") -> int:
    status = 0

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    await asyncio.wait_for(app(_scope(path), receive, send), timeout=REQUEST_TIMEOUT)
    return status


def _in_own_loop(app: Veloce, path: str = "/health") -> tuple[int | None, str | None]:
    """Serve one request on a loop of this thread's own. Returns (status, error)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_request(app, path)), None
    except Exception as exc:  # noqa: BLE001 - the failure itself is the result
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        loop.close()


def _concurrently(app: Veloce, threads: int, path: str = "/health") -> list[tuple]:
    """Fire one simultaneous request per thread, each on its own event loop."""
    gate = threading.Barrier(threads)
    results: list[tuple[int | None, str | None]] = [(None, None)] * threads

    def worker(slot: int) -> None:
        gate.wait()
        results[slot] = _in_own_loop(app, path)

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(REQUEST_TIMEOUT * 2)
    return results


def _app_with_warmup(counter: list[int]) -> Veloce:
    app = Veloce(openapi_url=None)

    @app.before_first_request
    async def warm_up() -> None:
        counter[0] += 1
        # Real warm-up - a pool, a cache, a key set - is not instant, and the
        # latch is held for however long it takes.
        await asyncio.sleep(HOOK_SECONDS)

    @app.get("/health", name="health")
    async def health() -> dict:
        return {"ok": True}

    return app


# ── the before_first_request latch ───────────────────────────────────


def test_simultaneous_first_requests_on_separate_loops_all_succeed():
    """Three loops reach the latch at once; none may be answered with an error.

    The `asyncio.Lock` this latch used binds to the first loop that blocks on
    it, so the second waiter bound it and the third was refused with
    `RuntimeError: ... is bound to a different event loop` - a 500 to that
    caller.
    """
    counter = [0]
    app = _app_with_warmup(counter)

    results = _concurrently(app, threads=3)

    errors = [err for _, err in results if err is not None]
    assert errors == [], f"first requests failed on other loops: {errors}"
    assert [status for status, _ in results] == [200, 200, 200]


def test_the_first_request_hooks_run_once_across_every_loop():
    """`before_first_request` means once per app, not once per event loop."""
    counter = [0]
    app = _app_with_warmup(counter)

    _concurrently(app, threads=3)

    assert counter[0] == 1, f"hooks ran {counter[0]} times"


def test_a_later_request_on_a_fresh_loop_still_succeeds():
    """The latch must not leave state bound to the loop that happened to win."""
    counter = [0]
    app = _app_with_warmup(counter)

    _concurrently(app, threads=3)
    status, err = _in_own_loop(app)

    assert err is None, f"a request on a new loop failed after warm-up: {err}"
    assert status == 200
    assert counter[0] == 1


def test_a_failing_first_request_hook_still_reports_to_its_caller():
    """A hook that raises must surface, not hang the request behind the latch."""
    app = Veloce(openapi_url=None)

    @app.before_first_request
    async def explode() -> None:
        raise RuntimeError("warm-up failed")

    @app.get("/health", name="health")
    async def health() -> dict:
        return {"ok": True}

    status, err = _in_own_loop(app)

    # Veloce turns an unhandled error into a 500; either way it must answer.
    assert status == 500 or err is not None


# ── the lazy OpenAPI registration ────────────────────────────────────


def test_the_openapi_flag_is_not_set_before_the_routes_are_registered(monkeypatch):
    """The flag is what other loops read to decide setup is finished.

    Setting it first tells a second loop "done" while the radix tree is still
    being written, so that request dispatches against a half-built table.
    """
    observed: dict[str, bool] = {}
    real = docs_ui.setup_openapi_routes

    def spy(application, **kwargs):
        observed["flag_during_registration"] = application._openapi_setup
        return real(application, **kwargs)

    monkeypatch.setattr(docs_ui, "setup_openapi_routes", spy)

    app = Veloce()
    app._setup_openapi()

    assert observed["flag_during_registration"] is False, (
        "_openapi_setup was already True while the docs routes were still being registered"
    )
    assert app._openapi_setup is True


def test_a_second_caller_does_not_return_until_the_docs_routes_exist(monkeypatch):
    """Returning early is what lets a request reach a half-built route table."""
    registering = threading.Event()
    release = threading.Event()
    real = docs_ui.setup_openapi_routes

    def slow(application, **kwargs):
        registering.set()
        # Hold registration open so the second caller must decide what to do
        # while it is unfinished.
        release.wait(REQUEST_TIMEOUT)
        return real(application, **kwargs)

    monkeypatch.setattr(docs_ui, "setup_openapi_routes", slow)

    app = Veloce()

    first = threading.Thread(target=app._setup_openapi)
    first.start()
    assert registering.wait(REQUEST_TIMEOUT), "registration never started"

    second = threading.Thread(target=app._setup_openapi)
    second.start()
    # The second caller must still be waiting: the routes do not exist yet.
    second.join(0.2)
    returned_early = not second.is_alive()

    release.set()
    first.join(REQUEST_TIMEOUT)
    second.join(REQUEST_TIMEOUT)

    assert not returned_early, (
        "_setup_openapi() returned while another caller was still registering the docs routes"
    )
    assert app.match("GET", "/openapi.json") is not None


@pytest.mark.parametrize("path", ["/openapi.json", "/docs"])
def test_concurrent_setup_registers_each_docs_route_exactly_once(path: str):
    """Two loops may both reach the lazy setup; it must still register once."""
    app = Veloce()

    @app.get("/health", name="health")
    async def health() -> dict:
        return {"ok": True}

    results = _concurrently(app, threads=4, path=path)

    errors = [err for _, err in results if err is not None]
    assert errors == [], f"docs requests failed: {errors}"
    assert all(status == 200 for status, _ in results), [s for s, _ in results]
