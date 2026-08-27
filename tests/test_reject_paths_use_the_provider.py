"""A pre-dispatch refusal is encoded in the app's JSON dialect, like every other body.

An app that configures a JSON provider gets it on every response — a handler
return, a 404, a 405, a 418, a 422. Three refusals did not: the ASGI 413, the
ASGI 400, and the native 413.

The two ASGI ones build a `JSONResponse`, which resolves the provider through
`dumps_current` — but they run *before* dispatch binds the app contextvar, so it
found nothing and fell back to the direct encoder. The native one called
`orjson.dumps` outright.

The ASGI half was the sharper of the two, because the failure was not consistent.
`dumps_current` reads a contextvar, so whether the dialect appeared depended on
whether an earlier request had happened to leave it bound on the same task:

    each request in its own task   -> {"detail": "Request body exceeds ...
    all four in one shared task    -> {"dialect": "custom", "detail": "Request ...

Same app, same request, output shape decided by scheduling. Under a real server
every request gets its own task, so the practical answer was "the dialect is
lost" — but a test that reused a task would have shown it present.

All three sites hold the app already (`self`, or `self.app`), so they encode with
`dumps_for(app, payload)`. The app was never missing; it just was not bound yet,
which is why fixing `dumps_current`'s fallback would have been the wrong repair.
"""

from __future__ import annotations

import asyncio
import json

from veloce import Veloce
from veloce.json_provider import DefaultJSONProvider
from veloce.serving.protocol import HttpProtocol

LIMIT = 10
CRLF = b"\r\n"


class ShoutingProvider(DefaultJSONProvider):
    """Stamps every object it encodes, so a bypass is unmistakable."""

    def dumps(self, obj, **kwargs):
        if isinstance(obj, dict):
            obj = {"dialect": "custom", **obj}
        return super().dumps(obj, **kwargs)


def _app(**config) -> Veloce:
    app = Veloce(openapi_url=None)
    app.json_provider_class = ShoutingProvider
    app.config["MAX_CONTENT_LENGTH"] = LIMIT
    app.config.update(config)

    @app.post("/up")
    async def up(request) -> dict:
        return {"got": len(await request.body())}

    @app.get("/hi")
    async def hi() -> dict:
        return {"ok": True}

    return app


def _scope(path: str, body: bytes = b"", method: str = "GET", query: bytes = b"") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": [(b"content-length", str(len(body)).encode())] if body else [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
        "scheme": "http",
        "root_path": "",
    }


async def _drive(app, scope: dict, body: bytes = b"") -> tuple[int, bytes]:
    """One request, in its own task, so no contextvar can leak into it."""
    out: dict = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    await asyncio.create_task(app(scope, receive, send))
    return out["status"], out["body"]


def _native_413(app) -> bytes:
    class _Transport(asyncio.Transport):
        def __init__(self) -> None:
            super().__init__()
            self.writes: list[bytes] = []
            self.closed = False

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        def close(self) -> None:
            self.closed = True

        def is_closing(self) -> bool:
            return self.closed

        def get_extra_info(self, name, default=None):
            return default

    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(app, loop)
        transport = _Transport()
        proto.connection_made(transport)
        head = b"POST /up HTTP/1.1" + CRLF + b"Host: t" + CRLF
        head += b"Content-Length: 100" + CRLF + CRLF
        proto.data_received(head + b"x" * 100)
        for _ in range(4):
            loop.run_until_complete(asyncio.sleep(0))
        return b"".join(transport.writes).partition(CRLF + CRLF)[2]
    finally:
        loop.close()


# ── the baseline: what the dialect already reached ───────────────────


async def test_a_handler_response_carries_the_dialect():
    _status, body = await _drive(_app(), _scope("/hi"))
    assert json.loads(body)["dialect"] == "custom"


async def test_a_404_carries_the_dialect():
    status, body = await _drive(_app(), _scope("/nope"))
    assert status == 404
    assert json.loads(body)["dialect"] == "custom"


async def test_a_405_carries_the_dialect():
    status, body = await _drive(_app(), _scope("/hi", method="POST"))
    assert status == 405
    assert json.loads(body)["dialect"] == "custom"


# ── the three that did not ───────────────────────────────────────────


async def test_the_asgi_413_carries_the_dialect():
    """The defect: it fell back to the direct encoder before dispatch bound the app."""
    status, body = await _drive(_app(), _scope("/up", b"x" * 100, "POST"), b"x" * 100)
    assert status == 413
    assert json.loads(body)["dialect"] == "custom"


async def test_the_asgi_413_still_carries_its_payload():
    _status, body = await _drive(_app(), _scope("/up", b"x" * 100, "POST"), b"x" * 100)
    parsed = json.loads(body)
    assert parsed["detail"] == "Request body exceeds MAX_CONTENT_LENGTH"
    assert parsed["status_code"] == 413
    assert parsed["limit"] == LIMIT


async def test_the_asgi_400_carries_the_dialect():
    """A malformed query string is refused before a `Request` exists."""
    status, body = await _drive(_app(), _scope("/hi", query=b"\xff\xfe"))
    assert status == 400
    assert json.loads(body)["dialect"] == "custom"


def test_the_native_413_carries_the_dialect():
    """The defect: `orjson.dumps` outright, with `self.app` in scope."""
    parsed = json.loads(_native_413(_app()))
    assert parsed["dialect"] == "custom"


def test_the_native_413_still_carries_its_payload():
    parsed = json.loads(_native_413(_app()))
    assert parsed["detail"] == "Request body exceeds MAX_CONTENT_LENGTH"
    assert parsed["limit"] == LIMIT


def test_both_transports_send_the_same_413_under_a_provider():
    """The parity claim, retested with a dialect - the test that was missing."""
    app = _app()
    loop = asyncio.new_event_loop()
    try:
        _status, asgi_body = loop.run_until_complete(
            _drive(app, _scope("/up", b"x" * 100, "POST"), b"x" * 100)
        )
    finally:
        loop.close()
    assert json.loads(asgi_body) == json.loads(_native_413(_app()))


# ── the failure was scheduling-dependent; it must not be ─────────────


async def test_the_413_is_the_same_whether_a_request_preceded_it():
    """The sharp edge: the shape used to depend on a leaked contextvar."""
    app = _app()

    # Cold - nothing has bound the app contextvar on this task.
    _s1, cold = await _drive(app, _scope("/up", b"x" * 100, "POST"), b"x" * 100)

    # Warm - a normal request first, in the *same* task, which used to leak.
    async def warm_then_reject():
        out: dict = {"body": b""}

        async def receive():
            return {"type": "http.request", "body": b"x" * 100, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                out["body"] += message.get("body", b"")

        async def noop_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def noop_send(message):
            return None

        await app(_scope("/hi"), noop_receive, noop_send)
        await app(_scope("/up", b"x" * 100, "POST"), receive, send)
        return out["body"]

    warm = await asyncio.create_task(warm_then_reject())
    assert json.loads(cold) == json.loads(warm)


async def test_the_413_is_stable_across_repeats():
    """The same app answers identically each time, on one app instance.

    This was parametrized over `range(3)` with the value unused, so it built a
    fresh app per run and asserted the same thing three times under three ids -
    which does not test repetition at all. Driving one app three times does.
    """
    app = _app()
    bodies = [
        (await _drive(app, _scope("/up", b"x" * 100, "POST"), b"x" * 100))[1] for _ in range(3)
    ]
    assert all(json.loads(body)["dialect"] == "custom" for body in bodies)
    assert len({body for body in bodies}) == 1, f"the 413 body varied across repeats: {bodies}"


# ── an app with no provider is unchanged ─────────────────────────────


def _plain_app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = LIMIT

    @app.post("/up")
    async def up(request) -> dict:
        return {"got": len(await request.body())}

    return app


async def test_a_plain_app_gets_no_stamp_on_the_asgi_413():
    status, body = await _drive(_plain_app(), _scope("/up", b"x" * 100, "POST"), b"x" * 100)
    assert status == 413
    assert "dialect" not in json.loads(body)


def test_a_plain_app_gets_no_stamp_on_the_native_413():
    assert "dialect" not in json.loads(_native_413(_plain_app()))


async def test_a_plain_app_still_refuses_correctly():
    _status, body = await _drive(_plain_app(), _scope("/up", b"x" * 100, "POST"), b"x" * 100)
    assert json.loads(body) == {
        "detail": "Request body exceeds MAX_CONTENT_LENGTH",
        "status_code": 413,
        "limit": LIMIT,
    }


async def test_a_body_under_the_limit_is_still_accepted():
    """The negative case for the refusal itself."""
    status, body = await _drive(_plain_app(), _scope("/up", b"x" * 5, "POST"), b"x" * 5)
    assert status == 200
    assert json.loads(body) == {"got": 5}


# ── a sort-order provider, not just a stamping one ───────────────────


async def test_json_sort_keys_reaches_the_413():
    """A second, independent provider setting - the stamp could be a special case."""
    app = _plain_app()
    app.config["JSON_SORT_KEYS"] = True
    _status, body = await _drive(app, _scope("/up", b"x" * 100, "POST"), b"x" * 100)
    text = body.decode()
    assert text.index('"detail"') < text.index('"limit"') < text.index('"status_code"')


def test_json_sort_keys_reaches_the_native_413():
    app = _plain_app()
    app.config["JSON_SORT_KEYS"] = True
    text = _native_413(app).decode()
    assert text.index('"detail"') < text.index('"limit"') < text.index('"status_code"')
