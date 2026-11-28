"""`Veloce(root_path=...)` sets the prefix the app believes it is mounted under.

The deployment guide tells you to use it: "You can also set the prefix on the
application directly: `app = Veloce(root_path="/api")`". The constructor
accepted it, stored it on `app.root_path`, and nothing in the framework ever
read it — so an app configured exactly as documented generated unprefixed
external URLs and redirected to unprefixed paths, which behind a proxy that
strips the prefix means a redirect out of the application.

The ASGI server still wins when it supplies one: it knows where the app was
actually mounted, and the constructor argument is a declaration made before that
is known.
"""

from __future__ import annotations

import asyncio

import orjson
import pytest

from tests._asgi_drive import http_scope
from veloce import Request, Veloce
from veloce.testclient import TestClient


def _app(**kwargs) -> Veloce:
    app = Veloce(openapi_url=None, **kwargs)

    @app.get("/info")
    async def info(request: Request):
        return {
            "root_path": request.root_path,
            "script_root": request.script_root,
            "external": request.url_for("info", _external=True),
        }

    @app.get("/slashed/")
    async def slashed():
        return {}

    return app


# ── the parameter now reaches every surface ──────────────────────────


def test_the_request_reports_the_configured_root_path():
    """The defect: this was the empty string."""
    assert TestClient(_app(root_path="/api")).get("/info").json()["root_path"] == "/api"


def test_script_root_follows_it():
    assert TestClient(_app(root_path="/api")).get("/info").json()["script_root"] == "/api"


def test_an_external_url_carries_the_prefix():
    body = TestClient(_app(root_path="/api")).get("/info").json()
    assert body["external"] == "http://testserver/api/info"


def test_a_slash_redirect_carries_the_prefix():
    """Without it, a proxy that strips the prefix redirects out of the app."""
    response = TestClient(_app(root_path="/api")).get("/slashed", follow_redirects=False)
    assert response.headers["location"] == "/api/slashed/"


def test_an_app_at_root_is_unchanged():
    body = TestClient(_app()).get("/info").json()
    assert body["root_path"] == ""
    assert body["script_root"] == ""
    assert body["external"] == "http://testserver/info"


# ── the server still wins ────────────────────────────────────────────


def test_the_asgi_scope_beats_the_constructor():
    """The server knows where the app was actually mounted."""
    app = _app(root_path="/declared")
    seen = {}

    async def drive():
        scope = http_scope(
            type="http",
            method="GET",
            path="/info",
            raw_path=b"/info",
            query_string=b"",
            headers=[(b"host", b"testserver")],
            root_path="/from-server",
            scheme="http",
            client=("127.0.0.1", 1234),
            server=("testserver", 80),
        )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                seen["body"] = message.get("body", b"")

        await app(scope, receive, send)

    asyncio.run(drive())
    assert orjson.loads(seen["body"])["root_path"] == "/from-server"


def test_an_empty_scope_root_path_falls_back_to_the_constructor():
    """An ASGI server that is not behind a prefix sets `""`, not absent."""
    app = _app(root_path="/api")
    seen = {}

    async def drive():
        scope = http_scope(
            type="http",
            method="GET",
            path="/info",
            raw_path=b"/info",
            query_string=b"",
            headers=[(b"host", b"testserver")],
            root_path="",
            scheme="http",
            client=("127.0.0.1", 1234),
            server=("testserver", 80),
        )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                seen["body"] = message.get("body", b"")

        await app(scope, receive, send)

    asyncio.run(drive())
    assert orjson.loads(seen["body"])["root_path"] == "/api"


# ── normalisation ────────────────────────────────────────────────────


@pytest.mark.parametrize("given", ["/api", "api", "/api/", "api/", "//api//"])
def test_equivalent_spellings_normalise_to_one_shape(given):
    """A trailing slash would double the separator in every URL built from it."""
    assert Veloce(openapi_url=None, root_path=given).root_path == "/api"


@pytest.mark.parametrize("given", ["", "/", "//"])
def test_a_rootless_spelling_normalises_to_empty(given):
    assert Veloce(openapi_url=None, root_path=given).root_path == ""


def test_a_normalised_prefix_builds_one_separator():
    body = TestClient(_app(root_path="/api/")).get("/info").json()
    assert body["external"] == "http://testserver/api/info"


def test_a_multi_segment_prefix_is_kept_whole():
    assert Veloce(openapi_url=None, root_path="/a/b/c").root_path == "/a/b/c"
    body = TestClient(_app(root_path="/a/b/c")).get("/info").json()
    assert body["external"] == "http://testserver/a/b/c/info"


# ── what must not change ─────────────────────────────────────────────


def test_routing_still_matches_the_unprefixed_path():
    """`root_path` describes where the app is mounted, not what it routes."""
    client = TestClient(_app(root_path="/api"))
    assert client.get("/info").status_code == 200
    assert client.get("/api/info").status_code == 404


def test_a_proxy_fix_prefix_still_wins_for_script_root():
    """A trusted outer-edge prefix outranks both."""
    app = _app(root_path="/api")

    @app.get("/proxied")
    async def proxied(request: Request):
        request.state["proxy_fix_prefix"] = "/edge"
        return {"script_root": request.script_root}

    assert TestClient(app).get("/proxied").json()["script_root"] == "/edge"
