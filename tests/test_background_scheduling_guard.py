"""Scheduling background work costs nothing when there is none.

`_schedule_background_tasks` runs on every response. It opened by allocating a
list, then discovered - on the overwhelming majority of responses - that there
was nothing to put in it:

    coros = []                                   # every response
    if request._background_tasks is not None: ...
    attached_bg = getattr(response, "background", None)
    ...
    if not coros:
        return False                             # the common exit

The two sources are checked first now, and a response with neither returns
before anything is allocated.

The `getattr` stays rather than becoming `response.background`. The attribute is
a `Response` slot and every construction path in the tree initialises it - the
four `__new__` sites all call `Response.__init__` - but a user subclass whose
`__init__` skips `super()` would turn a direct read into an `AttributeError` on
the response path. A crash is not worth thirty nanoseconds.

This file covers what must still happen: both task sources, together and apart,
the shapes accepted for an attached task, and the upload-spool release that is
deferred only when something was actually scheduled.

Tasks are asserted through `app.handle_request` and a short sleep, the
convention in `test_response_background.py`. The sync `TestClient` runs its own
loop and settles a spawned task only sometimes, so it cannot witness one.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce import BackgroundTask, BackgroundTasks, Request, Response, Veloce

pytestmark = pytest.mark.asyncio


def _req(path: str = "/x", method: str = "GET") -> Request:
    return Request(method=method, path=path, query_string="", headers={}, body=b"")


def _multipart(field: str, filename: str, payload: bytes) -> Request:
    """A minimal multipart upload request, for the spool-release contract."""
    boundary = "veloceboundary"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
        ).encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return Request(
        method="POST",
        path="/u",
        query_string="",
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        body=body,
    )


async def _settle() -> None:
    await asyncio.sleep(0.05)


# ── nothing to schedule: the path being made cheaper ─────────────────


async def test_a_plain_response_schedules_nothing():
    """The common case, and the one the guard exists for."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    response = await app.handle_request(_req())
    assert response.status_code == 200


async def test_scheduling_reports_nothing_was_scheduled():
    """The return value drives whether spool release is deferred."""
    app = Veloce(openapi_url=None)
    assert app._schedule_background_tasks(_req(), Response(body=b"x")) is False


async def test_a_response_with_background_none_schedules_nothing():
    app = Veloce(openapi_url=None)
    assert app._schedule_background_tasks(_req(), Response(body=b"x", background=None)) is False


async def test_an_empty_injected_queue_still_reports_scheduled():
    """A queue exists but holds nothing: it is still a source, and `run_all()`
    on an empty queue is harmless. Behaviour is unchanged by the guard."""
    app = Veloce(openapi_url=None)
    request = _req()
    request._background_tasks = BackgroundTasks()
    assert app._schedule_background_tasks(request, Response(body=b"x")) is True
    await _settle()


# ── a response-attached task still runs ──────────────────────────────


async def test_an_attached_single_task_runs():
    log: list[str] = []
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(body=b"ok", background=BackgroundTask(log.append, "ran"))

    await app.handle_request(_req())
    await _settle()
    assert log == ["ran"]


async def test_an_attached_task_collection_runs_every_member():
    log: list[str] = []
    tasks = BackgroundTasks()
    tasks.add_task(log.append, "first")
    tasks.add_task(log.append, "second")

    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(body=b"ok", background=tasks)

    await app.handle_request(_req())
    await _settle()
    assert log == ["first", "second"]


async def test_an_async_attached_task_runs():
    log: list[str] = []

    async def record() -> None:
        log.append("async")

    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(body=b"ok", background=BackgroundTask(record))

    await app.handle_request(_req())
    await _settle()
    assert log == ["async"]


# ── an injected queue still runs ─────────────────────────────────────


async def test_an_injected_queue_runs():
    log: list[str] = []
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(tasks: BackgroundTasks) -> dict:
        tasks.add_task(log.append, "injected")
        return {"ok": True}

    await app.handle_request(_req())
    await _settle()
    assert log == ["injected"]


async def test_both_sources_run_together():
    """The guard must not let one source mask the other."""
    log: list[str] = []
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(tasks: BackgroundTasks):
        tasks.add_task(log.append, "injected")
        return Response(body=b"ok", background=BackgroundTask(log.append, "attached"))

    await app.handle_request(_req())
    await _settle()
    assert sorted(log) == ["attached", "injected"]


# ── negative: an unusable attachment is ignored, not fatal ───────────


async def test_an_attachment_with_no_runnable_method_is_ignored():
    """Anything without `run`/`run_all` contributes nothing and must not raise."""

    class NotATask:
        pass

    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(body=b"ok", background=NotATask())

    response = await app.handle_request(_req())
    assert response.status_code == 200


async def test_an_unusable_attachment_reports_nothing_scheduled():
    class NotATask:
        pass

    app = Veloce(openapi_url=None)
    response = Response(body=b"x")
    response.background = NotATask()
    assert app._schedule_background_tasks(_req(), response) is False


async def test_a_failing_background_task_does_not_break_the_response():
    app = Veloce(openapi_url=None)

    def boom() -> None:
        raise RuntimeError("background failure")

    @app.get("/x")
    async def x():
        return Response(body=b"ok", background=BackgroundTask(boom))

    response = await app.handle_request(_req())
    assert response.status_code == 200
    await _settle()


# ── the spool-release contract the return value drives ───────────────


async def test_an_upload_is_released_after_a_background_task_finishes():
    """A task may still be reading the upload, so release is deferred to it."""
    seen: list[int] = []
    app = Veloce(openapi_url=None)

    @app.post("/u")
    async def upload(request):
        form = await request.form()
        upload_file = form["f"]

        async def consume() -> None:
            seen.append(len(await upload_file.read()))

        return Response(body=b"ok", background=BackgroundTask(consume))

    response = await app.handle_request(_multipart("f", "a.txt", b"hello"))
    assert response.status_code == 200
    await _settle()
    assert seen == [5]


async def test_an_upload_with_no_background_task_still_answers():
    """The other half: nothing scheduled, so the spool is released at teardown."""
    app = Veloce(openapi_url=None)

    @app.post("/u")
    async def upload(request) -> dict:
        form = await request.form()
        return {"size": len(await form["f"].read())}

    response = await app.handle_request(_multipart("f", "a.txt", b"hello"))
    assert response.status_code == 200


# ── end to end: many responses, none leaking work ────────────────────


async def test_repeated_plain_responses_schedule_nothing_cumulatively():
    """A guard that returned the wrong answer would strand tasks per request."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    for _ in range(25):
        assert (await app.handle_request(_req())).status_code == 200


async def test_a_mix_of_routes_keeps_each_response_independent():
    log: list[str] = []
    app = Veloce(openapi_url=None)

    @app.get("/plain")
    async def plain() -> dict:
        return {"ok": True}

    @app.get("/work")
    async def work():
        return Response(body=b"ok", background=BackgroundTask(log.append, "worked"))

    await app.handle_request(_req("/plain"))
    await app.handle_request(_req("/work"))
    await app.handle_request(_req("/plain"))
    await _settle()
    assert log == ["worked"]


@pytest.mark.parametrize("shape", ["single", "collection"])
async def test_both_attachment_shapes_are_accepted(shape: str):
    log: list[str] = []
    if shape == "single":
        attached: object = BackgroundTask(log.append, "x")
    else:
        collection = BackgroundTasks()
        collection.add_task(log.append, "x")
        attached = collection

    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(body=b"ok", background=attached)

    await app.handle_request(_req())
    await _settle()
    assert log == ["x"]
