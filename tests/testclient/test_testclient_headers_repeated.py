"""Repeated request headers through the test clients.

HTTP lets a header appear more than once - two `Authorization` headers, a
proxy chain spread across several `X-Forwarded-For` lines - and the framework
reads them that way: `headers.get` answers the first, `headers.getlist` answers
all of them. The clients typed `headers=` as a mapping, which cannot carry a
name twice, so a sequence of pairs collapsed on the way in and the last value
was the only one that arrived.

That made behaviour which depends on repetition untestable through the clients:
the duplicate-`Authorization` handling below could only be exercised by
building an ASGI scope by hand, and reading it through a client suggested the
framework kept the last header when in fact the client had dropped the first.
"""

from __future__ import annotations

from veloce import AsyncTestClient, Request, TestClient, Veloce


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/seen")
    async def seen(request: Request) -> dict:
        return {
            "get": request.headers.get("authorization"),
            "getlist": request.headers.getlist("authorization"),
            "forwarded": request.headers.getlist("x-forwarded-for"),
        }

    return app


PAIRS = [
    ("authorization", "Bearer first"),
    ("authorization", "Bearer second"),
    ("x-forwarded-for", "10.0.0.1"),
    ("x-forwarded-for", "10.0.0.2"),
]


def test_pairs_keep_every_repeat():
    with TestClient(_app()) as client:
        body = client.get("/seen", headers=PAIRS).json()
    assert body["getlist"] == ["Bearer first", "Bearer second"]
    assert body["forwarded"] == ["10.0.0.1", "10.0.0.2"]


def test_the_first_of_a_repeated_header_is_what_get_answers():
    """The documented reading, now reachable through the client."""
    with TestClient(_app()) as client:
        body = client.get("/seen", headers=PAIRS).json()
    assert body["get"] == "Bearer first"


async def test_the_async_client_agrees():
    """Both clients share the normaliser, so neither can drift from the other."""
    async with AsyncTestClient(_app()) as client:
        body = (await client.get("/seen", headers=PAIRS)).json()
    assert body["getlist"] == ["Bearer first", "Bearer second"]
    assert body["get"] == "Bearer first"


def test_a_mapping_still_works():
    """The negative: the shape every existing test passes is unchanged."""
    with TestClient(_app()) as client:
        body = client.get("/seen", headers={"authorization": "Bearer only"}).json()
    assert body["get"] == "Bearer only"
    assert body["getlist"] == ["Bearer only"]


def test_a_per_call_header_still_replaces_a_base_one():
    """A client-level header is overridden, not duplicated, by a per-call one."""
    with TestClient(_app()) as client:
        client._base_headers["authorization"] = "Bearer base"
        body = client.get("/seen", headers={"authorization": "Bearer call"}).json()
    assert body["getlist"] == ["Bearer call"]


def test_repeats_within_one_call_survive_a_base_header():
    with TestClient(_app()) as client:
        client._base_headers["authorization"] = "Bearer base"
        body = client.get(
            "/seen",
            headers=[("authorization", "Bearer a"), ("authorization", "Bearer b")],
        ).json()
    assert body["getlist"] == ["Bearer a", "Bearer b"]


def test_a_body_request_still_gets_its_content_type():
    """The content type is defaulted without disturbing the pairs beside it."""
    app = Veloce(openapi_url=None)

    @app.post("/echo")
    async def echo(request: Request) -> dict:
        return {
            "ct": request.headers.get("content-type"),
            "getlist": request.headers.getlist("x-tag"),
        }

    with TestClient(app) as client:
        body = client.post(
            "/echo", json={"a": 1}, headers=[("x-tag", "one"), ("x-tag", "two")]
        ).json()
    assert body["ct"].startswith("application/json")
    assert body["getlist"] == ["one", "two"]


def test_an_explicit_content_type_is_not_overridden_by_the_default():
    app = Veloce(openapi_url=None)

    @app.post("/echo")
    async def echo(request: Request) -> dict:
        return {"ct": request.headers.get("content-type")}

    with TestClient(app) as client:
        body = client.post(
            "/echo", content=b"{}", headers=[("content-type", "application/vnd.api+json")]
        ).json()
    assert body["ct"] == "application/vnd.api+json"
