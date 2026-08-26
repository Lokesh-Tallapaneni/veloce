"""`Cookie()`-marked list parameters, and what a repeated cookie name yields.

The module header used to say these "collect every repeated cookie value" and
the test below was named `test_repeated_cookies_collected` - while its own
docstring and its assertion both said the opposite, and the assertion is the one
that is right: a repeated name collapses to the **first** occurrence.

Cookies are not query parameters. A `Cookie` header carries one namespace of
name/value pairs (RFC 6265 Sec. 4.2.1), and where a name appears more than once
the first is taken - unlike `?tag=a&tag=b`, where a list marker really does
collect every value. Naming the cookie behaviour after the query behaviour is
how someone comes to expect a list and ship code that silently sees one item.
"""

from __future__ import annotations

import asyncio

import orjson

from veloce import Cookie, Query, Veloce
from veloce.testclient import TestClient


def _run_http(app: Veloce, path: str, cookie_header: str) -> bytes:
    """Drive one HTTP request through the ASGI surface, returning the body."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"cookie", cookie_header.encode())],
        "scheme": "http",
    }
    incoming = [{"type": "http.request", "body": b"", "more_body": False}]
    sent: list[dict] = []

    async def receive() -> dict:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app(scope, receive, send))
    finally:
        loop.close()
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def test_single_cookie_value_is_one_item_list():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(pref: list[str] = Cookie(default=[])):
        return {"pref": pref}

    with TestClient(app) as client:
        client.cookies["pref"] = "dark"
        resp = client.get("/x")

    assert resp.json() == {"pref": ["dark"]}


def test_missing_optional_cookie_list_uses_default():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(pref: list[str] = Cookie(default=["light"])):
        return {"pref": pref}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.json() == {"pref": ["light"]}


def test_missing_required_cookie_list_is_422():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(pref: list[str] = Cookie()):
        return {"pref": pref}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.status_code == 422


def test_a_repeated_cookie_name_yields_only_the_first_value():
    """A list-marked cookie does not collect duplicates - the first wins.

    Named for what it asserts. It was `test_repeated_cookies_collected`, which
    claims the opposite of the line below it.
    """
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(tag: list[str] = Cookie(default=[])):
        return {"tag": tag}

    body = _run_http(app, "/x", "tag=a; tag=b; tag=c")
    assert orjson.loads(body) == {"tag": ["a"]}


# ── stated against the query-parameter behaviour it was named for ────
#
# The contradiction was not arbitrary: `?tag=a&tag=b` on a list-marked *query*
# parameter really does collect both. Asserting the two side by side is what
# makes the cookie rule memorable, and stops the names converging again.


def test_a_repeated_query_parameter_does_collect_every_value():
    app = Veloce(openapi_url=None)

    @app.get("/q")
    async def q(tag: list[str] = Query(default=[])):
        return {"tag": tag}

    with TestClient(app) as client:
        assert client.get("/q?tag=a&tag=b&tag=c").json() == {"tag": ["a", "b", "c"]}


def test_the_two_markers_differ_on_a_repeated_name():
    """The property, stated once: query collects, cookie takes the first."""
    app = Veloce(openapi_url=None)

    @app.get("/q")
    async def q(tag: list[str] = Query(default=[])):
        return {"tag": tag}

    @app.get("/c")
    async def c(tag: list[str] = Cookie(default=[])):
        return {"tag": tag}

    with TestClient(app) as client:
        from_query = client.get("/q?tag=a&tag=b").json()["tag"]
    from_cookie = orjson.loads(_run_http(app, "/c", "tag=a; tag=b"))["tag"]

    assert from_query == ["a", "b"]
    assert from_cookie == ["a"]
    assert from_query != from_cookie


def test_a_single_cookie_still_arrives_as_a_one_item_list():
    """The negative: taking the first must still produce a list, not a scalar."""
    app = Veloce(openapi_url=None)

    @app.get("/c")
    async def c(tag: list[str] = Cookie(default=[])):
        return {"tag": tag}

    assert orjson.loads(_run_http(app, "/c", "tag=only"))["tag"] == ["only"]
