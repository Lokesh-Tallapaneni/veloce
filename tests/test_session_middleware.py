"""SessionMiddleware — signed-cookie session round-trip and signing."""

from __future__ import annotations

import logging

from tests.conftest import make_request
from veloce import Request, SessionMiddleware, Veloce
from veloce.testclient import TestClient


class TestSessions:
    async def test_session_set_and_read(self):
        app = Veloce(openapi_url=None)
        app.add_middleware(SessionMiddleware(secret_key="test-secret-key"))

        @app.get("/set")
        async def set_session(request: Request):
            request.state["session"]["username"] = "alice"
            return {"ok": True}

        @app.get("/get")
        async def get_session(request: Request):
            return {"username": request.state.get("session", {}).get("username", "")}

        # Set session
        resp = await app.handle_request(make_request(path="/set"))
        assert resp.status_code == 200
        assert "Set-Cookie" in resp.headers

        # Extract cookie
        cookie = resp.headers["Set-Cookie"]
        cookie_val = cookie.split(";")[0].split("=", 1)[1]

        # Read session
        resp2 = await app.handle_request(
            make_request(path="/get", headers={"cookie": f"session={cookie_val}"})
        )
        import orjson

        data = orjson.loads(resp2.body)
        assert data["username"] == "alice"

    def test_session_signing(self):
        from veloce.middleware.sessions import SessionMiddleware

        mw = SessionMiddleware(secret_key="test")

        # Sign and verify via the underlying Signer.
        encoded = mw.encode_cookie({"user": "alice"})
        decoded = mw.decode_cookie(encoded)
        assert decoded["user"] == "alice"

        # A tampered cookie is refused.
        tampered = encoded[:-5] + "xxxxx"
        assert mw.decode_cookie(tampered) is None


# ── end to end through a client ───────────────────────────────
#
# Moved here from `test_security_middleware_e2e.py`, which covered three
# unrelated middleware subsystems end to end. These are that subsystem's.


def _session_app(max_cookie_size: int = 4093) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="x" * 32, max_cookie_size=max_cookie_size))

    @app.get("/big")
    async def big(request):
        request.session["payload"] = "A" * 8192
        return {"wrote": True}

    @app.get("/small")
    async def small(request):
        request.session["user_id"] = 42
        return {"wrote": True}

    @app.get("/read")
    async def read(request):
        return {"user_id": request.session.get("user_id")}

    return app


def test_session_oversize_payload_is_dropped_with_warning(caplog):
    """An 8 KB session payload exceeds the cookie ceiling — the middleware
    must emit a warning at `veloce.sessions` and refuse to set the cookie
    instead of corrupting the next request."""
    app = _session_app()
    with (
        TestClient(app) as client,
        caplog.at_level(logging.WARNING, logger="veloce.sessions"),
    ):
        resp = client.get("/big")

    assert resp.status_code == 200
    assert "Set-Cookie" not in resp.headers
    assert any(
        rec.name == "veloce.sessions" and rec.levelno == logging.WARNING for rec in caplog.records
    )


def test_session_small_payload_is_set_and_roundtrips():
    """A small payload fits under the ceiling — Set-Cookie is emitted and
    the cookie round-trips so the next request sees the same session."""
    app = _session_app()
    with TestClient(app) as client:
        first = client.get("/small")
        assert first.status_code == 200
        assert "Set-Cookie" in first.headers
        assert "session" in first.cookies

        second = client.get("/read")
        assert second.status_code == 200
        assert second.json() == {"user_id": 42}
