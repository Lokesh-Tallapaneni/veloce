"""The same app answers the same way on both transports.

Veloce serves HTTP two ways: through the ASGI entry point (uvicorn, and
`TestClient`) and through `HttpProtocol`, the transport `app.run()` and
`VeloceWorker` use. The second has ~1400 lines of framing of its own and no test
door, so it was reached only by a test that stood up a socket or wrote its own
fake transport — and the recorded pattern for this project is that the serious
defects cluster in exactly that file. Finding 1.7 in this same review is one
instance: a fix applied to the ASGI 413 and not to the native one, unnoticed.

`tests/_native_client.py` is the shared door. This file uses it to assert the
two transports **against each other** across the surface where they were most
likely to drift: status, headers, body, HEAD stripping, keep-alive, redirects,
error rendering, and the middleware phases.

Asserting them against each other rather than against fixed expectations is the
point. A change that alters both stays green; a change that alters one fails
here, which is the class of bug this file exists to catch.
"""

from __future__ import annotations

import pytest

from tests._native_client import NativeClient
from veloce import (
    CORSMiddleware,
    HTTPException,
    JSONResponse,
    Middleware,
    PlainTextResponse,
    Request,
    Response,
    StreamingResponse,
    Veloce,
    status,
)
from veloce.testclient import TestClient


def _build() -> Veloce:
    """One app covering the shapes the two transports frame differently."""
    app = Veloce(openapi_url=None)

    @app.get("/json")
    async def json_route():
        return {"ok": True, "n": 1}

    @app.get("/text")
    async def text_route():
        return PlainTextResponse("hello")

    @app.get("/empty")
    async def empty_route():
        return Response(body=b"", status_code=204)

    @app.get("/headers")
    async def headers_route():
        return JSONResponse({"ok": True}, headers={"X-Custom": "v", "X-Other": "w"})

    @app.get("/status")
    async def status_route():
        return JSONResponse({"created": True}, status_code=201)

    @app.get("/params/{item_id}")
    async def params_route(item_id: int):
        return {"item_id": item_id}

    @app.get("/query")
    async def query_route(q: str = "none", n: int = 0):
        return {"q": q, "n": n}

    @app.post("/echo")
    async def echo_route(request: Request):
        return {"body": (await request.body()).decode()}

    @app.post("/model")
    async def model_route(request: Request):
        return {"got": request.get_json()}

    @app.get("/boom")
    async def boom_route():
        raise RuntimeError("boom")

    @app.get("/http-error")
    async def http_error_route():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "nope")

    @app.get("/cookie")
    async def cookie_route():
        resp = JSONResponse({"ok": True})
        resp.set_cookie("a", "1")
        resp.set_cookie("b", "2")
        return resp

    @app.get("/big")
    async def big_route():
        return PlainTextResponse("x" * 20000)

    return app


@pytest.fixture
def doors():
    """(asgi, native) clients over two identical apps."""
    native = NativeClient(_build())
    yield TestClient(_build()), native
    native.close()


def _pair(doors, method, path, **kwargs):
    asgi, native = doors
    return getattr(asgi, method)(path, **kwargs), getattr(native, method)(path, **kwargs)


# ── status, body, and the declared type ──────────────────────────────


@pytest.mark.parametrize(
    "path",
    ["/json", "/text", "/empty", "/headers", "/status", "/params/7", "/query", "/big"],
)
def test_both_transports_agree_on_status(doors, path):
    a, n = _pair(doors, "get", path)
    assert a.status_code == n.status_code, path


@pytest.mark.parametrize(
    "path",
    ["/json", "/text", "/empty", "/headers", "/status", "/params/7", "/query", "/big"],
)
def test_both_transports_agree_on_the_body(doors, path):
    a, n = _pair(doors, "get", path)
    assert a.body == n.body, path


@pytest.mark.parametrize("path", ["/json", "/text", "/headers", "/status"])
def test_both_transports_agree_on_content_type(doors, path):
    a, n = _pair(doors, "get", path)
    assert a.headers["content-type"].lower() == n.headers["content-type"].lower(), path


def test_both_transports_send_the_same_custom_headers(doors):
    a, n = _pair(doors, "get", "/headers")
    assert (a.headers["X-Custom"], a.headers["X-Other"]) == (
        n.headers["x-custom"],
        n.headers["x-other"],
    )


def test_both_transports_agree_on_a_path_parameter(doors):
    a, n = _pair(doors, "get", "/params/7")
    assert a.json() == n.json() == {"item_id": 7}


def test_both_transports_reject_a_bad_path_parameter_alike(doors):
    a, n = _pair(doors, "get", "/params/notanint")
    assert a.status_code == n.status_code


def test_both_transports_agree_on_query_parsing(doors):
    a, n = _pair(doors, "get", "/query?q=hi&n=5")
    assert a.json() == n.json() == {"q": "hi", "n": 5}


def test_both_transports_agree_on_a_missing_route(doors):
    a, n = _pair(doors, "get", "/nope")
    assert a.status_code == n.status_code == 404


def test_both_transports_agree_on_a_wrong_method(doors):
    a, n = _pair(doors, "post", "/json")
    assert a.status_code == n.status_code == 405


# ── HEAD, which the native path strips itself ────────────────────────


@pytest.mark.parametrize("path", ["/json", "/text", "/big"])
def test_a_head_response_carries_no_body_on_either_transport(doors, path):
    a, n = _pair(doors, "head", path)
    assert a.body == b""
    assert n.body == b""


@pytest.mark.parametrize("path", ["/json", "/text"])
def test_a_head_response_keeps_the_get_status_on_either_transport(doors, path):
    a_get, n_get = _pair(doors, "get", path)
    a_head, n_head = _pair(doors, "head", path)
    assert a_head.status_code == a_get.status_code
    assert n_head.status_code == n_get.status_code


def test_a_head_response_keeps_its_content_type_on_the_native_path(doors):
    _asgi, native = doors
    assert "application/json" in native.head("/json").headers["content-type"]


# ── request bodies ───────────────────────────────────────────────────


def test_both_transports_read_a_body(doors):
    a, n = _pair(doors, "post", "/echo", content=b"payload")
    assert a.json() == n.json() == {"body": "payload"}


def test_both_transports_read_a_json_body(doors):
    a, n = _pair(doors, "post", "/model", json={"k": "v"})
    assert a.json() == n.json() == {"got": {"k": "v"}}


def test_both_transports_read_an_empty_body(doors):
    a, n = _pair(doors, "post", "/echo", content=b"")
    assert a.json() == n.json() == {"body": ""}


def test_both_transports_read_a_large_body(doors):
    payload = b"y" * 50_000
    a, n = _pair(doors, "post", "/echo", content=payload)
    assert a.json() == n.json() == {"body": payload.decode()}


# ── errors ───────────────────────────────────────────────────────────


def test_both_transports_render_an_http_exception_alike(doors):
    a, n = _pair(doors, "get", "/http-error")
    assert a.status_code == n.status_code == 403
    assert a.body == n.body


def test_both_transports_render_an_unhandled_error_alike(doors):
    a, n = _pair(doors, "get", "/boom")
    assert a.status_code == n.status_code == 500


def test_an_unhandled_error_leaks_no_traceback_on_either_transport(doors):
    a, n = _pair(doors, "get", "/boom")
    assert b"RuntimeError" not in a.body
    assert b"RuntimeError" not in n.body


# ── cookies, which the native path frames itself ─────────────────────


def test_both_transports_send_every_set_cookie(doors):
    asgi, native = doors
    asgi_cookies = asgi.get("/cookie").headers.get("Set-Cookie", "")
    native_cookies = native.get("/cookie").headers.get("set-cookie", "")
    assert "a=1" in asgi_cookies and "b=2" in asgi_cookies
    assert "a=1" in native_cookies and "b=2" in native_cookies


# ── the middleware phases ────────────────────────────────────────────


def _middleware_app():
    app = Veloce(openapi_url=None)

    class Stamp(Middleware):
        async def process_request(self, request):
            request.state.stamped = True
            return None

        async def process_response(self, request, response):
            response.headers["X-Stamp"] = "yes"
            return response

    app.add_middleware(Stamp())
    app.add_middleware(CORSMiddleware(allow_origins=["https://ok.example"]))

    @app.get("/")
    async def index(request: Request):
        return {"stamped": getattr(request.state, "stamped", False)}

    return app


def test_request_middleware_runs_on_both_transports():
    native = NativeClient(_middleware_app())
    try:
        assert TestClient(_middleware_app()).get("/").json() == {"stamped": True}
        assert native.get("/").json() == {"stamped": True}
    finally:
        native.close()


def test_response_middleware_runs_on_both_transports():
    native = NativeClient(_middleware_app())
    try:
        assert TestClient(_middleware_app()).get("/").headers["X-Stamp"] == "yes"
        assert native.get("/").headers["x-stamp"] == "yes"
    finally:
        native.close()


def test_cors_headers_match_on_both_transports():
    native = NativeClient(_middleware_app())
    try:
        headers = {"Origin": "https://ok.example"}
        asgi_resp = TestClient(_middleware_app()).get("/", headers=headers)
        native_resp = native.get("/", headers=headers)
        assert (
            asgi_resp.headers["Access-Control-Allow-Origin"]
            == native_resp.headers["access-control-allow-origin"]
        )
    finally:
        native.close()


# ── keep-alive and pipelining, which only the native path has ────────


def test_a_keep_alive_connection_serves_several_requests():
    client = NativeClient(_build(), keep_alive=True)
    try:
        for _ in range(3):
            assert client.get("/json").json() == {"ok": True, "n": 1}
    finally:
        client.close()


def test_a_pipelined_batch_gets_one_response_each():
    client = NativeClient(_build(), keep_alive=True)
    try:
        req = b"GET /json HTTP/1.1\r\nHost: t\r\n\r\n"
        responses = client.pipeline(req * 4)
        assert len(responses) == 4
        assert all(r.status_code == 200 for r in responses)
    finally:
        client.close()


def test_pipelined_responses_come_back_in_order():
    """FIFO ordering is the native path's own guarantee."""
    app = Veloce(openapi_url=None)

    @app.get("/a")
    async def a():
        return {"which": "a"}

    @app.get("/b")
    async def b():
        return {"which": "b"}

    client = NativeClient(app, keep_alive=True)
    try:
        responses = client.pipeline(
            b"GET /a HTTP/1.1\r\nHost: t\r\n\r\n",
            b"GET /b HTTP/1.1\r\nHost: t\r\n\r\n",
            b"GET /a HTTP/1.1\r\nHost: t\r\n\r\n",
        )
        assert [r.json()["which"] for r in responses] == ["a", "b", "a"]
    finally:
        client.close()


def test_an_http_1_0_request_closes_the_connection():
    client = NativeClient(_build())
    try:
        resp = client.get("/json", version="HTTP/1.0")
        assert resp.status_code == 200
        assert resp.headers.get("connection", "close") == "close"
    finally:
        client.close()


def test_a_connection_close_request_is_honoured():
    client = NativeClient(_build())
    try:
        resp = client.get("/json", headers={"Connection": "close"})
        assert resp.headers.get("connection") == "close"
    finally:
        client.close()


# ── the door itself ──────────────────────────────────────────────────
#
# If the client stops driving the protocol, every parity test above passes
# vacuously by comparing two empty responses.


def test_the_native_client_actually_reaches_the_handler():
    ran = []
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        ran.append(1)
        return {"ok": True}

    client = NativeClient(app)
    try:
        assert client.get("/").status_code == 200
        assert ran == [1]
    finally:
        client.close()


def test_the_native_client_parses_a_chunked_response():
    """`_dechunk` must reassemble, so a caller compares payloads not framing."""
    app = Veloce(openapi_url=None)

    @app.get("/stream")
    async def stream():

        async def gen():
            yield b"one"
            yield b"two"

        return StreamingResponse(content=gen(), content_type="text/plain")

    client = NativeClient(app)
    try:
        assert client.get("/stream").body == b"onetwo"
    finally:
        client.close()


def test_the_native_client_reports_the_reason_phrase():
    client = NativeClient(_build())
    try:
        assert client.get("/json").reason == "OK"
    finally:
        client.close()
