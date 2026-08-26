"""The upload-release callback holds the form, not the whole request.

A response with a background task attaches a done-callback that closes the
request's upload spool files once the last task finishes. The callback closed
over `request`, so the entire `Request` - headers, body bytes, `state`, the ASGI
scope and its `receive`/`send` callables - stayed reachable for as long as the
slowest background task ran. A background task is precisely the thing that
outlives its response, so that is the worst object to pin to one.

`_close_uploads` only ever needed `request._form`, so the callback now closes
over the form.

Retention is asserted with a `weakref`, not by reading the closure: what matters
is that the object becomes collectable, and a test that inspected
`__closure__` would pass just as happily if something else still held a
reference.
"""

from __future__ import annotations

import asyncio
import gc
import weakref

from veloce import BackgroundTask, Response, Veloce
from veloce.testclient import TestClient


class _Probe:
    """A weakref-able stand-in for the request's liveness.

    `Request` is slotted without `__weakref__`, so it cannot be referenced
    weakly. Parking this on `request.state` makes it reachable exactly while the
    request is: the state dict is a request attribute, so anything retaining the
    request retains this too.
    """


def _app(gate: asyncio.Event, seen: dict) -> Veloce:
    app = Veloce(openapi_url=None)

    @app.post("/upload")
    async def upload(request):
        form = await request.form()
        probe = _Probe()
        request.state.probe = probe
        seen["request_ref"] = weakref.ref(probe)
        seen["form_ref"] = weakref.ref(form)
        del probe

        async def slow():
            await gate.wait()

        return Response(
            body=b"ok",
            background=BackgroundTask(slow),
        )

    return app


def _files():
    return {"f": ("a.txt", b"hello world", "text/plain")}


def _request_with_form():
    """A request whose multipart form has already been parsed into a spool file."""
    import tempfile

    from tests.conftest import make_request
    from veloce.http.datastructures import FormData, UploadFile

    spool = tempfile.SpooledTemporaryFile(max_size=1)
    spool.write(b"hello world")
    spool.seek(0)
    request = make_request(path="/upload", method="POST")
    request._form = FormData([("f", UploadFile(filename="a.txt", file=spool))])
    return request


# ── the callback does not retain the request ─────────────────────────
#
# Asserted against `_schedule_background_tasks` directly. Driving it through a
# client cannot show this: the client, the response and the surrounding frames
# all keep the request reachable for the duration of the test, so the callback's
# reference is invisible among them. Called directly, with the only other
# reference dropped, the difference is exactly what is being measured.


async def test_the_release_callback_does_not_retain_the_request():
    """The defect: the callback closed over `request`, so a pending background
    task pinned the whole object - headers, body, state, scope and the ASGI
    callables - for as long as it ran."""
    app = Veloce(openapi_url=None)
    gate = asyncio.Event()

    async def slow():
        await gate.wait()

    request = _request_with_form()
    probe = _Probe()
    request.state.probe = probe
    probe_ref = weakref.ref(probe)
    del probe

    response = Response(body=b"ok", background=BackgroundTask(slow))
    assert app._schedule_background_tasks(request, response) is True

    del request, response
    for _ in range(3):
        gc.collect()
    assert probe_ref() is None, "a pending background task still pins the request"

    gate.set()
    await asyncio.sleep(0)


async def test_the_form_survives_so_the_files_can_still_be_closed():
    """The other half: dropping the request must not drop what the callback
    needs, or the spool files would never be closed."""
    app = Veloce(openapi_url=None)
    gate = asyncio.Event()

    async def slow():
        await gate.wait()

    request = _request_with_form()
    form_ref = weakref.ref(request._form)
    handle = request._form["f"].file

    response = Response(body=b"ok", background=BackgroundTask(slow))
    app._schedule_background_tasks(request, response)
    del request, response
    for _ in range(3):
        gc.collect()

    assert form_ref() is not None
    assert not handle.closed

    gate.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if handle.closed:
            break
    assert handle.closed


# ── and the files are still closed when the task finishes ────────────


def test_the_spool_files_are_closed_after_the_task():
    """The behaviour the callback exists for, which the change must preserve."""
    app = Veloce(openapi_url=None)
    handles: list = []

    @app.post("/upload")
    async def upload(request):
        form = await request.form()
        handles.append(form["f"].file)

        async def noop():
            return None

        return Response(body=b"ok", background=BackgroundTask(noop))

    with TestClient(app) as client:
        client.post("/upload", files=_files())

    assert handles and handles[0].closed


def test_the_files_are_closed_even_when_the_task_fails():
    """A failed task still counts as done, so it cannot strand the files."""
    app = Veloce(openapi_url=None)
    handles: list = []

    @app.post("/upload")
    async def upload(request):
        form = await request.form()
        handles.append(form["f"].file)

        async def boom():
            raise RuntimeError("task failed")

        return Response(body=b"ok", background=BackgroundTask(boom))

    with TestClient(app) as client:
        client.post("/upload", files=_files())

    assert handles and handles[0].closed


def test_several_tasks_close_the_files_once_the_last_finishes():
    app = Veloce(openapi_url=None)
    handles: list = []
    ran: list[int] = []

    @app.post("/upload")
    async def upload(request):
        from veloce import BackgroundTasks

        form = await request.form()
        handles.append(form["f"].file)

        tasks = BackgroundTasks()
        for index in range(3):

            async def one(index=index):
                ran.append(index)

            tasks.add_task(one)
        return Response(body=b"ok", background=tasks)

    with TestClient(app) as client:
        client.post("/upload", files=_files())

    assert sorted(ran) == [0, 1, 2]
    assert handles and handles[0].closed


def test_a_request_without_an_upload_needs_no_release():
    """The guard: no form means no callback, and nothing to close."""
    app = Veloce(openapi_url=None)
    ran: list[str] = []

    @app.post("/plain")
    async def plain(request):
        async def noop():
            ran.append("task")

        return Response(body=b"ok", background=BackgroundTask(noop))

    with TestClient(app) as client:
        assert client.post("/plain", content=b"{}").status_code == 200
    assert ran == ["task"]
