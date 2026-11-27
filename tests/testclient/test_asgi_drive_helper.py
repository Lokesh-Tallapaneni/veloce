"""The shared raw-ASGI drive behaves like the copies it replaces.

`tests/_asgi_drive.py` is test infrastructure, so it needs its own tests: a
helper that silently built a wrong scope would make every module using it fail
somewhere unrelated, or - worse - pass for the wrong reason.
"""

from __future__ import annotations

import pytest

from tests._asgi_drive import body_of, drive, headers_of, http_scope, status_of
from veloce import Request, Veloce


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    @app.get("/where")
    async def where(request: Request):
        return {"path": request.path, "query": request.query_string}

    @app.post("/echo")
    async def echo(request: Request):
        return {"body": (await request.body()).decode()}

    return app


# ── the scope ────────────────────────────────────────────────────────


def test_the_default_scope_is_a_get_of_root():
    scope = http_scope()
    assert scope["type"] == "http"
    assert scope["method"] == "GET"
    assert scope["path"] == "/"


@pytest.mark.parametrize(
    "key",
    ["type", "http_version", "method", "path", "raw_path", "query_string", "headers", "scheme"],
)
def test_every_key_an_app_may_read_is_present(key):
    """The reason for a builder: an omitted key surfaces as a KeyError from
    inside the framework rather than as a failed assertion."""
    assert key in http_scope()


def test_an_override_is_applied():
    assert http_scope(method="POST")["method"] == "POST"


def test_raw_path_follows_path():
    """They disagreeing is its own class of bug, so it is not left to callers."""
    assert http_scope(path="/items/1")["raw_path"] == b"/items/1"


def test_an_explicit_raw_path_wins():
    scope = http_scope(path="/a b", raw_path=b"/a%20b")
    assert scope["raw_path"] == b"/a%20b"


def test_the_builder_returns_a_fresh_dict():
    """Shared defaults must not be mutable across calls."""
    first = http_scope()
    first["method"] = "DELETE"
    assert http_scope()["method"] == "GET"


# ── the drive ────────────────────────────────────────────────────────


async def test_it_returns_the_messages_the_app_sent():
    messages = await drive(_app())
    assert [m["type"] for m in messages][:2] == ["http.response.start", "http.response.body"]


async def test_the_accessors_read_the_response():
    messages = await drive(_app())
    assert status_of(messages) == 200
    assert b'"ok"' in body_of(messages)
    assert "application/json" in headers_of(messages)["content-type"]


async def test_overrides_reach_the_handler():
    messages = await drive(_app(), path="/where", query_string=b"a=1")
    assert b'"/where"' in body_of(messages)
    assert b'"a=1"' in body_of(messages)


async def test_a_body_reaches_the_handler():
    messages = await drive(_app(), method="POST", path="/echo", body=b"hello")
    assert b"hello" in body_of(messages)


async def test_chunks_are_delivered_as_one_body():
    messages = await drive(_app(), method="POST", path="/echo", chunks=[b"a", b"b", b"c"])
    assert b"abc" in body_of(messages)


async def test_a_prebuilt_scope_is_used():
    messages = await drive(_app(), http_scope(path="/where"))
    assert b'"/where"' in body_of(messages)


async def test_overrides_apply_on_top_of_a_prebuilt_scope():
    messages = await drive(_app(), http_scope(), path="/where")
    assert b'"/where"' in body_of(messages)


async def test_headers_reach_the_app():
    messages = await drive(_app(), headers=[(b"accept-encoding", b"gzip"), (b"host", b"x")])
    assert status_of(messages) == 200


async def test_a_missing_route_is_a_404():
    """The negative: the driver reports what happened rather than raising."""
    assert status_of(await drive(_app(), path="/nowhere")) == 404


async def test_receive_after_the_body_reports_disconnect():
    """A handler that keeps reading must see the stream end, not hang."""
    app = Veloce(openapi_url=None)
    seen = []

    @app.post("/drain")
    async def drain(request: Request):
        async for chunk in request.stream():
            seen.append(chunk)
        return {"n": len(seen)}

    app.add_route("/drain", drain, methods=["POST"], stream=True)
    messages = await drive(app, method="POST", path="/drain", chunks=[b"x", b"y"])
    assert status_of(messages) == 200
