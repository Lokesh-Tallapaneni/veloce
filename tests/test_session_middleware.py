"""SessionMiddleware — signed-cookie session round-trip and signing."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, SessionMiddleware, Veloce


class TestSessions:
    @pytest.mark.asyncio
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
