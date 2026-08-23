"""A multipart upload's spool file is released when the request is done with it.

An upload past the spool threshold is a real file on disk. Nothing closed it
once the response was sent, so the descriptor and the temp file survived until
the garbage collector happened to reach the `UploadFile` - descriptor
exhaustion under sustained upload traffic.

A background task outlives the response and may still be reading the upload, so
when one is scheduled it owns the release instead of teardown. Both orderings
are pinned here, because getting the second one wrong closes a file out from
under a running task.
"""

from __future__ import annotations

import asyncio

from veloce import BackgroundTask, JSONResponse, Veloce
from veloce.http.formparsers import MULTIPART_SPOOL_MAX_SIZE
from veloce.testclient import TestClient

_BOUNDARY = "spool-probe"
_HEADERS = {"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"}


def _payload(size: int) -> bytes:
    head = (
        f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="doc"; filename="x.bin"\r\n\r\n'
    ).encode()
    return head + b"x" * size + f"\r\n--{_BOUNDARY}--\r\n".encode()


def _app(seen: dict, *, background: bool) -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

    @app.post("/u")
    async def upload(request):
        form = await request.form()
        handle = form["doc"]
        seen["file"] = handle.file
        if not background:
            return {"n": handle.size}

        async def later():
            await asyncio.sleep(0.02)
            seen["open_during_task"] = not handle.file.closed
            handle.file.seek(0)
            seen["read_during_task"] = len(handle.file.read(16))

        return JSONResponse({"n": handle.size}, background=BackgroundTask(later))

    return app


def test_a_rolled_over_upload_is_closed_after_the_response():
    """The leak: this file stayed open until the collector reached it."""
    seen: dict = {}
    with TestClient(_app(seen, background=False)) as client:
        assert (
            client.post(
                "/u", content=_payload(MULTIPART_SPOOL_MAX_SIZE + 4096), headers=_HEADERS
            ).status_code
            == 200
        )
    assert seen["file"].closed


def test_a_small_in_memory_upload_is_closed_too():
    seen: dict = {}
    with TestClient(_app(seen, background=False)) as client:
        assert client.post("/u", content=_payload(64), headers=_HEADERS).status_code == 200
    assert seen["file"].closed


async def test_a_background_task_can_still_read_the_upload():
    """Closing at teardown would pull the file out from under the task."""
    seen: dict = {}
    app = _app(seen, background=True)
    # The wait happens inside the client context: leaving it shuts the app down,
    # which cancels the very background task this is about.
    async with app.async_test_client() as client:
        response = await client.post(
            "/u", content=_payload(MULTIPART_SPOOL_MAX_SIZE + 4096), headers=_HEADERS
        )
        assert response.status_code == 200
        assert seen["file"].closed is False, "released before the background task ran"

        for _ in range(200):
            await asyncio.sleep(0.01)
            if seen["file"].closed:
                break

    assert seen["open_during_task"] is True
    assert seen["read_during_task"] == 16
    assert seen["file"].closed, "never released after the background task finished"


def test_a_request_with_no_upload_is_unaffected():
    app = Veloce(openapi_url=None)

    @app.post("/plain")
    async def plain():
        return {"ok": True}

    with TestClient(app) as client:
        assert client.post("/plain", json={"a": 1}).status_code == 200


def test_the_upload_path_leaves_no_resource_warning():
    """The census that surfaced this: 34 unclosed-file warnings on this path."""
    import warnings

    seen: dict = {}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        with TestClient(_app(seen, background=False)) as client:
            client.post("/u", content=_payload(MULTIPART_SPOOL_MAX_SIZE + 4096), headers=_HEADERS)
    unclosed = [w for w in caught if "Unclosed file" in str(w.message)]
    assert not unclosed, [str(w.message) for w in unclosed]
