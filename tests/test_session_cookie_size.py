"""SessionMiddleware — Set-Cookie size guard (RFC 6265 §6.1)."""

from __future__ import annotations

import logging

from tests._sessions import seeded_session_response
from tests.conftest import make_request
from veloce import Request, Response, SessionMiddleware, TestClient, Veloce


def _req() -> Request:
    return make_request(method="GET", path="/x", query_string="", headers={}, body=b"")


async def test_oversized_session_logs_and_drops_cookie(caplog):
    """An ~8 KB session payload must log a warning and skip Set-Cookie
    rather than raising — a raise re-enters the middleware via the error
    response and would surface as an unhandled ASGI exception."""
    mw = SessionMiddleware(secret_key="k" * 32)
    request, response = seeded_session_response({"blob": "x" * 8192})

    with caplog.at_level(logging.WARNING, logger="veloce.sessions"):
        result = await mw.process_response(request, response)

    assert not any(h[0].lower() == "set-cookie" for h in result.headers.items())
    matches = [r for r in caplog.records if r.name == "veloce.sessions"]
    assert matches, "expected a warning on veloce.sessions"
    msg = matches[-1].getMessage()
    assert "max_cookie_size" in msg
    assert "session" in msg
    assert "ServerSessionMiddleware" in msg


async def test_oversized_session_honours_custom_limit(caplog):
    """A relaxed `max_cookie_size` should allow what the default would reject —
    no warning logged, cookie present."""
    mw = SessionMiddleware(secret_key="k" * 32, max_cookie_size=16_384)
    request, response = seeded_session_response({"blob": "x" * 8192})

    with caplog.at_level(logging.WARNING, logger="veloce.sessions"):
        result = await mw.process_response(request, response)

    assert any(h[0].lower() == "set-cookie" for h in result.headers.items())
    assert not [r for r in caplog.records if r.name == "veloce.sessions"]


async def test_small_session_stays_under_default_limit():
    """A small payload still round-trips through Set-Cookie unchanged."""
    mw = SessionMiddleware(secret_key="k" * 32)
    request, response = seeded_session_response({"user": "alice"})

    result = await mw.process_response(request, response)
    assert any(h[0].lower() == "set-cookie" for h in result.headers.items())


def test_max_cookie_size_constructor_param_defaults_to_4093():
    mw = SessionMiddleware(secret_key="k" * 32)
    assert mw.max_cookie_size == 4093


def test_oversized_session_end_to_end_returns_200_no_cookie(caplog):
    """End-to-end: a handler that stuffs 8KB into the session must get a
    clean 200 with no Set-Cookie, plus a warning in the log. This catches
    the re-entrant bug where raising re-runs response middleware on the
    error response, which still references the oversized session."""
    app = Veloce()
    app.add_middleware(SessionMiddleware(secret_key="k" * 32))

    @app.get("/big")
    async def big(request: Request) -> Response:
        request.state["session"]["blob"] = "x" * 8192
        request.state["session"].modified = True
        return Response(200, b"ok")

    with (
        caplog.at_level(logging.WARNING, logger="veloce.sessions"),
        TestClient(app) as client,
    ):
        resp = client.get("/big")

    assert resp.status_code == 200
    assert resp.body == b"ok"
    assert "set-cookie" not in {k.lower() for k in resp.headers}
    matches = [r for r in caplog.records if r.name == "veloce.sessions"]
    assert matches, "expected a warning on veloce.sessions"
    assert "max_cookie_size" in matches[-1].getMessage()
