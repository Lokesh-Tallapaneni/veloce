"""FileResponse honours a `Range:` request header.

`Accept-Ranges: bytes` is advertised on every file response; a single
satisfiable `bytes=` range is answered `206` with a `Content-Range`, one past
the end of the file `416`, and anything the server will not act on - an
unparsable header, a unit other than `bytes`, a multi-range request, a stale
`If-Range` validator - falls back to the full `200` representation
(RFC 9110 Sec. 14.2).

`GET` is the only method range handling is defined for, so a `Range` on any
other - `HEAD` included - is ignored outright.

Every behaviour is exercised through both construction forms, the sync
`FileResponse(path)` and `await FileResponse.from_path(path)`, which must agree.
"""

from __future__ import annotations

import asyncio
import os
from email.utils import formatdate
from pathlib import Path

import pytest

from tests.conftest import make_request
from veloce import FileResponse, Request, Response, TestClient, Veloce
from veloce._internal import _current_request_var
from veloce.helpers import (
    async_send_file,
    send_file,
    send_from_directory,
    send_from_directory_async,
)
from veloce.http.response import _INLINE_READ_MAX

#: A 100-byte body, so `Content-Range` totals in the assertions read directly.
BODY = bytes(range(100))

#: Fixed so `Last-Modified` / `If-Range` dates are predictable.
MTIME = 1_700_000_000

pytestmark = pytest.mark.filterwarnings(
    # The sync form warns about its blocking read on the running loop. That
    # deprecation is pinned by `tests/test_deprecation_warnings.py`; here it is
    # noise on the path this file deliberately exercises.
    "ignore::veloce._warnings.VeloceDeprecationWarning"
)


@pytest.fixture
def served(tmp_path: Path) -> Path:
    path = tmp_path / "payload.bin"
    path.write_bytes(BODY)
    os.utime(str(path), (MTIME, MTIME))
    return path


@pytest.fixture(params=["sync", "async"])
def client(request: pytest.FixtureRequest, served: Path):
    """A one-route app serving `served` through one of the two forms."""
    form = request.param
    app = Veloce()
    target = str(served)

    if form == "sync":

        @app.get("/file")
        async def serve_sync(req: Request) -> Response:
            return FileResponse(target, request=req)

    else:

        @app.get("/file")
        async def serve_async(req: Request) -> Response:
            return await FileResponse.from_path(target, request=req)

    with TestClient(app) as c:
        yield c


def _range(client, value: str, **headers: str):
    return client.get("/file", headers={"Range": value, **headers})


# ── Satisfiable ranges ────────────────────────────────────


def test_closed_range_returns_206_slice(client):
    resp = _range(client, "bytes=0-9")
    assert resp.status_code == 206
    assert resp.body == BODY[:10]
    assert len(resp.body) == 10
    assert resp.headers["Content-Range"] == "bytes 0-9/100"
    assert resp.headers["Content-Length"] == "10"


def test_mid_file_range(client):
    resp = _range(client, "bytes=20-29")
    assert resp.status_code == 206
    assert resp.body == BODY[20:30]
    assert resp.headers["Content-Range"] == "bytes 20-29/100"


def test_suffix_range_returns_tail(client):
    resp = _range(client, "bytes=-10")
    assert resp.status_code == 206
    assert resp.body == BODY[-10:]
    assert resp.headers["Content-Range"] == "bytes 90-99/100"


def test_suffix_range_longer_than_file_returns_whole_file(client):
    """RFC 9110 Sec. 14.1.2: a suffix length past the size clamps to the size."""
    resp = _range(client, "bytes=-500")
    assert resp.status_code == 206
    assert resp.body == BODY
    assert resp.headers["Content-Range"] == "bytes 0-99/100"


def test_open_ended_range_returns_remainder(client):
    resp = _range(client, "bytes=90-")
    assert resp.status_code == 206
    assert resp.body == BODY[90:]
    assert resp.headers["Content-Range"] == "bytes 90-99/100"


def test_end_past_eof_clamps_to_last_byte(client):
    resp = _range(client, "bytes=95-500")
    assert resp.status_code == 206
    assert resp.body == BODY[95:]
    assert resp.headers["Content-Range"] == "bytes 95-99/100"


def test_whole_file_range_is_still_206(client):
    resp = _range(client, "bytes=0-99")
    assert resp.status_code == 206
    assert resp.body == BODY
    assert resp.headers["Content-Range"] == "bytes 0-99/100"


def test_single_byte_range(client):
    resp = _range(client, "bytes=50-50")
    assert resp.status_code == 206
    assert resp.body == BODY[50:51]
    assert resp.headers["Content-Range"] == "bytes 50-50/100"


def test_accept_ranges_advertised_on_partial_and_full(client):
    assert client.get("/file").headers["Accept-Ranges"] == "bytes"
    assert _range(client, "bytes=0-9").headers["Accept-Ranges"] == "bytes"


# ── Unsatisfiable and ignored ranges ──────────────────────


def test_range_past_eof_returns_416(client):
    resp = _range(client, "bytes=500-600")
    assert resp.status_code == 416
    assert resp.headers["Content-Range"] == "bytes */100"
    assert resp.body == b""


def test_start_exactly_at_eof_returns_416(client):
    resp = _range(client, "bytes=100-")
    assert resp.status_code == 416
    assert resp.headers["Content-Range"] == "bytes */100"


def test_zero_length_suffix_returns_416(client):
    resp = _range(client, "bytes=-0")
    assert resp.status_code == 416
    assert resp.headers["Content-Range"] == "bytes */100"


def test_416_still_advertises_accept_ranges_and_validators(client):
    resp = _range(client, "bytes=500-600")
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["ETag"].startswith('W/"')
    assert resp.headers["Last-Modified"] == formatdate(MTIME, usegmt=True)


@pytest.mark.parametrize(
    "value",
    [
        "bytes=abc",
        "bytes=-",
        "bytes=",
        "0-9",
        "bytes=x-y",
    ],
)
def test_malformed_range_is_ignored(client, value):
    """RFC 9110 Sec. 14.2: an unparsable `Range` is ignored, not rejected."""
    resp = _range(client, value)
    assert resp.status_code == 200
    assert resp.body == BODY
    assert "Content-Range" not in resp.headers


def test_unknown_range_unit_is_ignored(client):
    resp = _range(client, "items=0-9")
    assert resp.status_code == 200
    assert resp.body == BODY
    assert "Content-Range" not in resp.headers


def test_multi_range_serves_the_full_representation(client):
    """Multi-range is deliberately unsupported: no `multipart/byteranges` body.

    Serving the whole representation is the sanctioned fallback for a `Range` the
    recipient will not act on, and matches what `StaticFiles` does.
    """
    resp = _range(client, "bytes=0-9,20-29")
    assert resp.status_code == 200
    assert resp.body == BODY
    assert "Content-Range" not in resp.headers


def test_caller_can_opt_out_with_accept_ranges_none(served: Path):
    """A caller-supplied `Accept-Ranges` is honoured, not just advertised."""
    app = Veloce()

    @app.get("/file")
    async def serve(req: Request) -> Response:
        return FileResponse(str(served), headers={"Accept-Ranges": "none"}, request=req)

    with TestClient(app) as c:
        resp = c.get("/file", headers={"Range": "bytes=0-9"})

    assert resp.status_code == 200
    assert resp.body == BODY
    assert resp.headers["Accept-Ranges"] == "none"


# ── If-Range (RFC 9110 Sec. 13.1.5) ───────────────────────


def test_if_range_matching_date_honours_the_range(client):
    resp = _range(client, "bytes=0-9", **{"If-Range": formatdate(MTIME, usegmt=True)})
    assert resp.status_code == 206
    assert resp.body == BODY[:10]


def test_if_range_stale_date_returns_full_body(client):
    resp = _range(client, "bytes=0-9", **{"If-Range": formatdate(MTIME - 3600, usegmt=True)})
    assert resp.status_code == 200
    assert resp.body == BODY
    assert "Content-Range" not in resp.headers


def test_if_range_weak_etag_returns_full_body(served: Path):
    """A client echoing the server's own advertised `ETag` gets the full body.

    This is the decided behaviour, not an oversight. `If-Range` is compared
    strongly (RFC 9110 Sec. 13.1.5) and the default file `ETag` is weak, so the
    comparison cannot succeed - and Sec. 13.1.5 also forbids a client from
    putting a weak tag in `If-Range` at all, which is what this request does.
    Serving the whole representation is the safe answer to it: honouring the
    range would splice bytes a weak validator never promised were identical.
    A resuming client uses `Last-Modified`, or a strong `ETag` the caller
    supplies - both of which have their own tests above and below.
    """
    app = Veloce()

    @app.get("/file")
    async def serve(req: Request) -> Response:
        return FileResponse(str(served), request=req)

    with TestClient(app) as c:
        etag = c.get("/file").headers["ETag"]
        resp = c.get("/file", headers={"Range": "bytes=0-9", "If-Range": etag})

    assert etag.startswith('W/"')
    assert resp.status_code == 200
    assert resp.status_code != 206
    assert resp.body == BODY
    assert "Content-Range" not in resp.headers


def test_if_range_matching_strong_etag_honours_the_range(served: Path):
    app = Veloce()

    @app.get("/file")
    async def serve(req: Request) -> Response:
        return FileResponse(str(served), headers={"ETag": '"v1"'}, request=req)

    with TestClient(app) as c:
        hit = c.get("/file", headers={"Range": "bytes=0-9", "If-Range": '"v1"'})
        miss = c.get("/file", headers={"Range": "bytes=0-9", "If-Range": '"v2"'})

    assert hit.status_code == 206
    assert hit.body == BODY[:10]
    assert miss.status_code == 200
    assert miss.body == BODY


# ── Regressions on the no-Range path ──────────────────────


def test_no_range_header_returns_the_whole_file(client):
    resp = client.get("/file")
    assert resp.status_code == 200
    assert resp.body == BODY
    assert resp.headers["Content-Length"] == "100"
    assert "Content-Range" not in resp.headers


def test_no_range_header_keeps_the_validator_headers(client):
    resp = client.get("/file")
    assert resp.headers["Last-Modified"] == formatdate(MTIME, usegmt=True)
    assert resp.headers["ETag"].startswith('W/"')


def test_head_sends_headers_without_a_body(client):
    resp = client.head("/file")
    assert resp.status_code == 200
    assert resp.body == b""
    assert resp.headers["Accept-Ranges"] == "bytes"


def test_head_ignores_range_and_describes_the_full_representation(client):
    """`HEAD` is not `GET`, so RFC 9110 Sec. 14.2 says ignore the `Range`.

    Pinned in both directions: no `206`, no `Content-Range`, and the
    `Content-Length` of the whole file - which is the number a client issues a
    `HEAD` on a download to learn, and which a `416` would have withheld.
    """
    resp = client.head("/file", headers={"Range": "bytes=0-9"})
    assert resp.status_code == 200
    assert resp.status_code != 206
    assert resp.body == b""
    assert "Content-Range" not in resp.headers
    assert resp.headers["Content-Length"] == "100"
    assert resp.headers["Accept-Ranges"] == "bytes"


def test_head_with_an_unsatisfiable_range_is_not_refused(client):
    resp = client.head("/file", headers={"Range": "bytes=500-600"})
    assert resp.status_code == 200
    assert resp.status_code != 416
    assert "Content-Range" not in resp.headers
    assert resp.headers["Content-Length"] == "100"


# ── Methods other than GET (RFC 9110 Sec. 14.2) ───────────


@pytest.fixture(params=["sync", "async"])
def write_client(request: pytest.FixtureRequest, served: Path):
    """The same file behind `POST` / `PUT` / `PATCH`, both construction forms."""
    form = request.param
    app = Veloce()
    target = str(served)

    if form == "sync":

        async def serve(req: Request) -> Response:
            return FileResponse(target, request=req)

    else:

        async def serve(req: Request) -> Response:
            return await FileResponse.from_path(target, request=req)

    app.add_route("/file", serve, methods=["POST", "PUT", "PATCH"])
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("method", ["post", "put", "patch"])
def test_a_range_on_a_non_get_method_is_ignored(write_client, method):
    """A client or proxy replaying a cached `Range` must not truncate a write."""
    resp = getattr(write_client, method)("/file", headers={"Range": "bytes=0-2"})
    assert resp.status_code == 200
    assert resp.status_code != 206
    assert resp.body == BODY
    assert "Content-Range" not in resp.headers


@pytest.mark.parametrize("method", ["post", "put", "patch"])
def test_an_unsatisfiable_range_on_a_non_get_method_is_not_refused(write_client, method):
    """The 416 is the damaging half: it answers the write with an empty body."""
    resp = getattr(write_client, method)("/file", headers={"Range": "bytes=500-600"})
    assert resp.status_code == 200
    assert resp.status_code != 416
    assert resp.body == BODY
    assert "Content-Range" not in resp.headers


def test_a_non_get_method_still_advertises_accept_ranges(write_client):
    """The advertisement is about the resource, not the method that fetched it."""
    assert write_client.post("/file").headers["Accept-Ranges"] == "bytes"


def test_a_reused_headers_dict_does_not_leak_content_range(served: Path):
    """The `Content-Range` lands on the response, never on the caller's dict."""
    shared: dict[str, str] = {}

    app = Veloce()

    @app.get("/file")
    async def serve(req: Request) -> Response:
        return FileResponse(str(served), headers=shared, request=req)

    with TestClient(app) as c:
        ranged = c.get("/file", headers={"Range": "bytes=0-9"})
        plain = c.get("/file")

    assert ranged.status_code == 206
    assert plain.status_code == 200
    assert "Content-Range" not in plain.headers
    assert "Content-Range" not in shared


# ── The streamed (large-file) path ────────────────────────


@pytest.fixture
def big_file(tmp_path: Path) -> Path:
    path = tmp_path / "big.bin"
    # Past `_INLINE_READ_MAX`, so a full response streams and a large slice of
    # it streams too - the only path that seeks before the first chunk read.
    path.write_bytes(bytes(i % 251 for i in range(_INLINE_READ_MAX * 2)))
    return path


def test_streamed_range_seeks_to_the_slice(big_file: Path):
    size = _INLINE_READ_MAX * 2
    start = _INLINE_READ_MAX // 2
    end = size - 1
    app = Veloce()

    @app.get("/big")
    async def serve(req: Request) -> Response:
        return await FileResponse.from_path(str(big_file), request=req)

    with TestClient(app) as c:
        resp = c.get("/big", headers={"Range": f"bytes={start}-{end}"})

    expected = big_file.read_bytes()[start : end + 1]
    assert resp.status_code == 206
    assert len(resp.body) == len(expected)
    assert resp.body == expected
    assert resp.headers["Content-Range"] == f"bytes {start}-{end}/{size}"
    assert resp.headers["Content-Length"] == str(len(expected))


def test_small_slice_of_a_big_file_is_buffered(big_file: Path):
    """The inline-read threshold applies to the slice, not the file it came from."""
    app = Veloce()

    @app.get("/big")
    async def serve(req: Request) -> Response:
        return await FileResponse.from_path(str(big_file), request=req)

    with TestClient(app) as c:
        resp = c.get("/big", headers={"Range": "bytes=10-19"})

    assert resp.status_code == 206
    assert resp.body == big_file.read_bytes()[10:20]
    assert resp.headers["Content-Range"] == f"bytes 10-19/{_INLINE_READ_MAX * 2}"


def test_streamed_full_file_is_unchanged(big_file: Path):
    size = _INLINE_READ_MAX * 2
    app = Veloce()

    @app.get("/big")
    async def serve(req: Request) -> Response:
        return await FileResponse.from_path(str(big_file), request=req)

    with TestClient(app) as c:
        resp = c.get("/big")

    assert resp.status_code == 200
    assert resp.body == big_file.read_bytes()
    assert resp.headers["Content-Length"] == str(size)
    assert "Content-Range" not in resp.headers


# ── Outside a request ─────────────────────────────────────


def test_constructed_outside_a_request_serves_the_whole_file(served: Path):
    """No request passed means no `Range` to honour - and none advertised.

    This used to assert `Accept-Ranges: bytes` here. A response that will
    ignore a `Range` should not advertise that it accepts one (RFC 9110
    Sec. 14.3): the client reads the advertisement, sends a `Range`, and spends
    a round trip discovering the full body it did not ask for. A caller serving
    its own ranges can still set the header explicitly.
    """
    resp = FileResponse(str(served))
    assert resp.status_code == 200
    assert resp.body == BODY
    assert "Accept-Ranges" not in resp.headers
    assert "Content-Range" not in resp.headers


def test_given_a_request_it_advertises_ranges(served: Path):
    """The opt-in path advertises what it will honour."""
    app = Veloce()

    @app.get("/file")
    async def serve(req: Request) -> Response:
        return FileResponse(str(served), request=req)

    with TestClient(app) as c:
        resp = c.get("/file")
    assert resp.headers["Accept-Ranges"] == "bytes"


async def test_from_path_outside_a_request_serves_the_whole_file(served: Path):
    resp = await FileResponse.from_path(str(served))
    assert resp.status_code == 200
    assert resp.body == BODY
    assert "Content-Range" not in resp.headers


# ── One headers dict, two files ───────────────────────────


def test_two_files_through_one_headers_dict_keep_their_own_validators(tmp_path: Path):
    """A shared headers dict must not pin the first file's validators.

    The dict is the shape a handler naturally writes - a module-level constant
    of app-wide headers - and aliasing it meant the second file shipped the
    first file's `ETag`. Behind `ConditionalMiddleware` a client holding that
    tag is told `304` for a body it has never seen.
    """
    shared = {"X-App": "1"}
    a = tmp_path / "a.bin"
    a.write_bytes(b"a" * 20)
    os.utime(str(a), (MTIME, MTIME))
    b = tmp_path / "b.bin"
    b.write_bytes(b"b" * 30)
    os.utime(str(b), (MTIME + 3600, MTIME + 3600))

    first = FileResponse(str(a), headers=shared)
    second = FileResponse(str(b), headers=shared)

    assert first.headers["ETag"] != second.headers["ETag"]
    assert first.headers["Last-Modified"] == formatdate(MTIME, usegmt=True)
    assert second.headers["Last-Modified"] == formatdate(MTIME + 3600, usegmt=True)
    assert first.headers["X-App"] == second.headers["X-App"] == "1"
    assert shared == {"X-App": "1"}


async def test_from_path_does_not_mutate_a_shared_headers_dict(tmp_path: Path):
    shared = {"X-App": "1"}
    a = tmp_path / "a.bin"
    a.write_bytes(b"a" * 20)
    b = tmp_path / "b.bin"
    b.write_bytes(b"b" * 30)

    first = await FileResponse.from_path(str(a), headers=shared)
    second = await FileResponse.from_path(str(b), headers=shared)

    assert first.headers["ETag"] != second.headers["ETag"]
    assert shared == {"X-App": "1"}


def test_a_caller_supplied_validator_still_wins(served: Path):
    """The copy must not break the existing override path."""
    resp = FileResponse(str(served), headers={"ETag": '"mine"', "Accept-Ranges": "none"})
    assert resp.headers["ETag"] == '"mine"'
    assert resp.headers["Accept-Ranges"] == "none"


# ── a response that is not the requested representation is not sliced ──


def test_an_error_handler_file_is_not_sliced_by_the_requests_range(tmp_path):
    """A `Range` applies to the representation the client asked for.

    An error page is a different one. Reproduced before this guard: a client
    resuming a download of a URL that 404s received the error page sliced to
    the requested byte offsets, or - for an offset past the page's length - a
    `416` with an empty body and the handler's content discarded entirely.
    """
    page = tmp_path / "404.html"
    page.write_text("<h1>not found</h1>")

    app = Veloce(openapi_url=None)

    @app.errorhandler(404)
    async def missing(request: Request, exc: Exception) -> Response:
        resp = FileResponse(str(page))
        resp.status_code = 404
        return resp

    client = TestClient(app)
    full = client.get("/nope")
    assert full.status_code == 404
    assert full.text == "<h1>not found</h1>"

    ranged = client.get("/nope", headers={"Range": "bytes=0-5"})
    assert ranged.status_code == 404
    assert ranged.text == "<h1>not found</h1>"
    assert "content-range" not in ranged.headers

    past_end = client.get("/nope", headers={"Range": "bytes=1000-1999"})
    assert past_end.status_code == 404
    assert past_end.text == "<h1>not found</h1>"


def test_the_send_file_helpers_do_not_slice_an_error_page(tmp_path):
    """The guard above covers the hand-built response, which never negotiates.

    `send_file` and `send_from_directory` opt in on the caller's behalf, so an
    error handler reaching for either got the case the comment says is exempt:
    the 404 page sliced to `bytes=0-3` - four bytes of HTML - and an offset
    past its length answered `416` with the handler's body discarded.
    """
    page = tmp_path / "404.html"
    page.write_text("<h1>not found</h1>")

    app = Veloce(openapi_url=None)

    @app.errorhandler(404)
    async def missing(request: Request, exc: Exception) -> Response:
        return send_file(str(page))

    client = TestClient(app)
    full = client.get("/nope")
    assert full.text == "<h1>not found</h1>"

    ranged = client.get("/nope", headers={"Range": "bytes=0-3"})
    assert ranged.status_code != 206
    assert ranged.text == "<h1>not found</h1>"
    assert "content-range" not in ranged.headers

    past_end = client.get("/nope", headers={"Range": "bytes=1000-1999"})
    assert past_end.status_code != 416
    assert past_end.text == "<h1>not found</h1>"


def test_send_from_directory_does_not_slice_an_error_page(tmp_path):
    page = tmp_path / "404.html"
    page.write_text("<h1>not found</h1>")

    app = Veloce(openapi_url=None)

    @app.errorhandler(404)
    async def missing(request: Request, exc: Exception) -> Response:
        return send_from_directory(str(tmp_path), "404.html")

    client = TestClient(app)
    ranged = client.get("/nope", headers={"Range": "bytes=0-3"})
    assert ranged.status_code != 206
    assert ranged.text == "<h1>not found</h1>"


def test_the_async_helpers_do_not_slice_an_error_page(tmp_path):
    page = tmp_path / "404.html"
    page.write_text("<h1>not found</h1>")

    app = Veloce(openapi_url=None)

    @app.errorhandler(404)
    async def missing(request: Request, exc: Exception) -> Response:
        return await async_send_file(str(page))

    with TestClient(app) as client:
        ranged = client.get("/nope", headers={"Range": "bytes=0-3"})
    assert ranged.status_code != 206
    assert ranged.text == "<h1>not found</h1>"


def test_send_from_directory_async_does_not_slice_an_error_page(tmp_path):
    page = tmp_path / "404.html"
    page.write_text("<h1>not found</h1>")

    app = Veloce(openapi_url=None)

    @app.errorhandler(404)
    async def missing(request: Request, exc: Exception) -> Response:
        return await send_from_directory_async(str(tmp_path), "404.html")

    with TestClient(app) as client:
        ranged = client.get("/nope", headers={"Range": "bytes=0-3"})
    assert ranged.status_code != 206
    assert ranged.text == "<h1>not found</h1>"


def test_a_sync_error_handler_also_leaves_the_page_whole(tmp_path):
    """A sync handler runs in the executor, which is a context boundary.

    `offload` runs it through a `copy_context()` snapshot, so the flag reaches
    it - but a copied context is exactly where an ambient flag can fall
    through, so it is asserted rather than assumed.
    """
    page = tmp_path / "404.html"
    page.write_text("<h1>not found</h1>")

    app = Veloce(openapi_url=None)

    @app.errorhandler(404)
    def missing(request: Request, exc: Exception) -> Response:
        return send_file(str(page))

    client = TestClient(app)
    ranged = client.get("/nope", headers={"Range": "bytes=0-3"})
    assert ranged.status_code != 206
    assert ranged.text == "<h1>not found</h1>"


def test_serving_an_error_page_does_not_disable_slicing_for_later_requests(tmp_path):
    """The flag must not outlive the handler that set it, in either direction."""
    page = tmp_path / "404.html"
    page.write_text("<h1>nf</h1>")
    served = tmp_path / "payload.bin"
    served.write_bytes(BODY)

    app = Veloce(openapi_url=None)

    @app.get("/file")
    async def file_route() -> Response:
        return send_file(str(served))

    @app.errorhandler(404)
    async def missing(request: Request, exc: Exception) -> Response:
        return send_file(str(page))

    client = TestClient(app)
    assert client.get("/file", headers={"Range": "bytes=0-9"}).status_code == 206
    assert client.get("/nope", headers={"Range": "bytes=0-3"}).status_code != 206
    # The real file still slices after an error page was served on the same task.
    after = client.get("/file", headers={"Range": "bytes=0-9"})
    assert after.status_code == 206
    assert after.body == BODY[:10]


def test_the_public_error_entry_point_also_leaves_the_page_whole(tmp_path):
    """`handle_user_exception` is a documented door for out-of-band callers.

    The suppression used to be set by the dispatch wrapper only, so a handler
    reached through the public entry point - what an MCP call or a background
    task uses - still sliced its error page. Both routes funnel through
    `_call_exc_handler`, which is where the flag belongs.
    """
    page = tmp_path / "404.html"
    page.write_text("<h1>not found</h1> and more besides")

    app = Veloce(openapi_url=None)

    @app.errorhandler(ValueError)
    async def on_value_error(request: Request, exc: Exception) -> Response:
        return send_file(str(page))

    async def drive() -> Response:
        request = make_request(headers={"range": "bytes=0-3"}, path="/anything")
        # Bound as dispatch binds it, or `send_file` finds no request at all
        # and skips negotiation for a reason that has nothing to do with the
        # flag under test.
        token = _current_request_var.set(request)
        try:
            return await app.handle_user_exception(ValueError("boom"), request)
        finally:
            _current_request_var.reset(token)

    response = asyncio.run(drive())
    assert response.status_code != 206
    assert response.body == page.read_bytes()
    assert "content-range" not in {k.lower() for k in response.headers}


def test_the_same_helper_still_slices_the_file_a_route_was_asked_for(tmp_path):
    """The negative: suppressing the slice on error pages must not suppress it
    where the file really is the representation the client requested."""
    served = tmp_path / "payload.bin"
    served.write_bytes(BODY)

    app = Veloce(openapi_url=None)

    @app.get("/file")
    async def file_route() -> Response:
        return send_file(str(served))

    client = TestClient(app)
    ranged = client.get("/file", headers={"Range": "bytes=0-9"})
    assert ranged.status_code == 206
    assert ranged.body == BODY[:10]
    assert ranged.headers["content-range"] == "bytes 0-9/100"


def test_a_route_file_is_still_refused_an_unsatisfiable_range(tmp_path):
    served = tmp_path / "payload.bin"
    served.write_bytes(BODY)

    app = Veloce(openapi_url=None)

    @app.get("/file")
    async def file_route() -> Response:
        return send_file(str(served))

    client = TestClient(app)
    assert client.get("/file", headers={"Range": "bytes=1000-1999"}).status_code == 416


def test_a_reused_bare_fileresponse_has_no_range_to_keep(tmp_path):
    """A response that never negotiated cannot carry a slice into a reuse.

    This is the weaker half of the reuse question and all that opt-in settles:
    a bare `FileResponse` ignores `Range`, so reusing it is safe by
    construction. The half it does NOT settle is the next test.
    """
    served = tmp_path / "shared.txt"
    served.write_text("0123456789")

    app = Veloce(openapi_url=None)
    built: list = []

    @app.get("/shared")
    async def shared(request: Request) -> Response:
        if not built:
            built.append(FileResponse(str(served)))
        return built[0]

    client = TestClient(app)
    first = client.get("/shared", headers={"Range": "bytes=0-2"})
    assert first.status_code == 200
    second = client.get("/shared")
    assert second.status_code == 200
    assert "content-range" not in second.headers


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known, filed: an OPTED-IN response that is cached and returned again keeps "
        "the first request's slice, because the range is resolved while the response "
        "is constructed. Opt-in fixed the error-handler case (a response standing in "
        "for a different resource) but not this one (the right resource, the wrong "
        "request). Closing it means resolving the slice at emit time - option (c) of "
        "the Task 1 decision - tracked as its own task in "
        "internal/plans/2026-10-22-filed-followups.md. Strict, so closing it fails "
        "here until this marker is removed."
    ),
)
def test_a_reused_opted_in_fileresponse_does_not_keep_the_first_range(tmp_path):
    """The half opt-in does not settle: a cached `send_file` keeps its slice.

    Measured: `Range: bytes=0-2` then a request with no `Range` at all answers
    `206 Content-Range: bytes 0-2/10` with three of ten bytes - a client that
    asked for the whole file gets a partial one.
    """
    served = tmp_path / "cached.txt"
    served.write_text("0123456789")

    app = Veloce(openapi_url=None)
    built: list = []

    @app.get("/cached")
    async def cached(request: Request) -> Response:
        if not built:
            built.append(FileResponse(str(served), request=request))
        return built[0]

    client = TestClient(app)
    assert client.get("/cached", headers={"Range": "bytes=0-2"}).status_code == 206
    second = client.get("/cached")
    assert second.status_code == 200
    assert "content-range" not in second.headers
    assert second.text == "0123456789"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known, filed: an error page returned as `(response, status)` rather than "
        "raised is still sliced. The suppression keys on an exception handler "
        "running, and this path never enters one - a handler sets the status "
        "itself. Same root cause as the cached-response case above, and the same "
        "fix closes both: resolve the slice at emit time, where the final status "
        "is known, instead of in `FileResponse.__init__`. Strict, so closing it "
        "fails here until this marker is removed."
    ),
)
def test_a_tuple_returned_error_page_is_not_sliced_either(tmp_path):
    """An error page returned with its status rather than raised.

    Measured: `return send_file(page), 404` with `Range: bytes=0-3` answers
    `404` with four bytes of HTML.
    """
    page = tmp_path / "404.html"
    page.write_text("<h1>not found</h1> and more text")

    app = Veloce(openapi_url=None)

    @app.get("/tuple-error")
    async def tuple_error():
        return send_file(str(page)), 404

    client = TestClient(app)
    whole = client.get("/tuple-error")
    ranged = client.get("/tuple-error", headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 404
    assert ranged.text == whole.text
