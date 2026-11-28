"""The declared-`Content-Length` early reject, and the cap that outlives it.

`MAX_CONTENT_LENGTH` is checked twice on the ASGI path: the **declared**
`Content-Length` is refused before a byte is read, and the **received** total is
checked as the body arrives. The declared check walked every raw header tuple on
every request - including bodiless `GET`s, which have no `Content-Length` and so
compared against all of them.

It now skips methods that do not carry a body. That is a change to a
security-relevant check, so what matters is that it removes only the *early*
reject and not the cap:

    GET with an over-limit body   -> still 413, from the received total
    GET under the limit          -> 200
    POST with an over-limit body -> still 413, now from the declared header

which is the same relationship a chunked body has always had - it omits
`Content-Length`, so only the running total can catch it.

Measured on techc: ~160ns saved per `GET` against ~28ns added per `POST`, on an
eight-header request.
"""

from __future__ import annotations

import pytest

from tests._asgi_drive import http_scope
from veloce import Veloce
from veloce.testclient import TestClient

_LIMIT = 100


@pytest.fixture
def client() -> TestClient:
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = _LIMIT

    @app.route("/any", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
    async def any_method(request):
        return {"n": len(await request.body())}

    @app.post("/streamed", stream=True)
    async def streamed(request):
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        return {"n": total}

    return client_for(app)


def client_for(app: Veloce) -> TestClient:
    return TestClient(app)


OVER = b"x" * (_LIMIT * 5)
UNDER = b"x" * 10


# ── the cap holds on every method, body-carrying or not ──────────────


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_body_carrying_method_is_refused(method, client):
    """The declared header still refuses these before the body is read."""
    assert client.request(method, "/any", content=OVER).status_code == 413


@pytest.mark.parametrize("method", ["GET", "OPTIONS"])
def test_a_bodiless_method_carrying_an_over_limit_body_is_still_refused(method, client):
    """The point of the change: the early reject is skipped, the cap is not."""
    assert client.request(method, "/any", content=OVER).status_code == 413


def test_the_streamed_path_still_refuses():
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = _LIMIT

    @app.post("/s", stream=True)
    async def s(request):
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        return {"n": total}

    assert TestClient(app).post("/s", content=OVER).status_code == 413


def test_an_undeclared_over_limit_body_is_refused_on_a_bodiless_method(client):
    """No `Content-Length` at all, so only the running total can catch it - the
    same situation the change puts every `GET` in."""
    resp = client.request("GET", "/any", stream=[b"x" * 60, b"x" * 60, b"x" * 60])
    assert resp.status_code == 413


# ── and an under-limit request is served, on every method ────────────
#
# The negatives: a cap that refused everything would pass every test above.


@pytest.mark.parametrize("method", ["GET", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
def test_an_under_limit_body_is_served(method, client):
    assert client.request(method, "/any", content=UNDER).json() == {"n": len(UNDER)}


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_request_with_no_body_is_served(method, client):
    assert client.request(method, "/any").status_code == 200


def test_a_head_request_is_served(client):
    assert client.request("HEAD", "/any").status_code == 200


def test_a_body_exactly_at_the_limit_is_served(client):
    assert client.post("/any", content=b"x" * _LIMIT).json() == {"n": _LIMIT}


def test_no_limit_configured_serves_a_large_body():
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = None

    @app.route("/any", methods=["GET", "POST"])
    async def any_method(request):
        return {"n": len(await request.body())}

    tc = TestClient(app)
    assert tc.post("/any", content=b"x" * 5000).json() == {"n": 5000}
    assert tc.request("GET", "/any", content=b"x" * 5000).json() == {"n": 5000}


# ── the two refusals answer identically ──────────────────────────────


def test_the_declared_and_received_refusals_agree(client):
    """A POST is refused by the declared header and a GET by the running total;
    a client must not be able to tell which fired."""
    declared = client.post("/any", content=OVER)
    received = client.request("GET", "/any", content=OVER)
    assert declared.status_code == received.status_code == 413
    assert declared.body == received.body


# ── what the early reject actually buys ──────────────────────────────
#
# Status alone cannot distinguish the two checks: both answer 413. What the
# declared check buys is that the body is *never read* - `receive()` is not
# called at all. That is the property worth keeping for body-carrying methods,
# and the one that is deliberately given up for bodiless ones.


class _CountingReceive:
    """A raw ASGI driver that counts how many times the body is pulled."""

    def __init__(self, app, method: str, body: bytes) -> None:
        self.app = app
        self.method = method
        self.body = body
        self.receives = 0
        self.status: int | None = None

    async def run(self) -> int:
        scope = http_scope(
            type="http",
            asgi={"version": "3.0"},
            http_version="1.1",
            method=self.method,
            path="/any",
            raw_path=b"/any",
            query_string=b"",
            headers=[
                (b"host", b"example.com"),
                (b"content-type", b"application/octet-stream"),
                (b"content-length", str(len(self.body)).encode()),
            ],
            client=("127.0.0.1", 1234),
            server=("127.0.0.1", 8000),
            scheme="http",
            root_path="",
        )
        sent = False

        async def receive():
            nonlocal sent
            self.receives += 1
            if not sent:
                sent = True
                return {"type": "http.request", "body": self.body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                self.status = message["status"]

        await self.app(scope, receive, send)
        return self.receives


def _limited_app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = _LIMIT

    @app.route("/any", methods=["GET", "POST"])
    async def any_method(request):
        return {"n": len(await request.body())}

    return app


async def test_an_over_limit_post_is_refused_without_reading_the_body():
    """The early reject, observed by its actual effect rather than its status."""
    driver = _CountingReceive(_limited_app(), "POST", OVER)
    reads = await driver.run()
    assert driver.status == 413
    assert reads == 0, "the declared check must refuse before the body is pulled"


async def test_an_over_limit_get_is_refused_after_reading():
    """The trade the change makes, stated explicitly: a bodiless method now
    reaches the received-total check, so it does read before refusing."""
    driver = _CountingReceive(_limited_app(), "GET", OVER)
    reads = await driver.run()
    assert driver.status == 413
    assert reads >= 1


async def test_an_under_limit_post_reads_normally():
    """The negative: the early reject must not be refusing everything."""
    driver = _CountingReceive(_limited_app(), "POST", UNDER)
    reads = await driver.run()
    assert driver.status == 200
    assert reads >= 1
