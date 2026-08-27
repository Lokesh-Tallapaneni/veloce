"""SessionMiddleware — timestamped signing and secret rotation (M8)."""

from __future__ import annotations

import time

import pytest

from veloce import Request, SessionMiddleware, Veloce


def _req(cookie: str = "") -> Request:
    headers = {"cookie": cookie} if cookie else {}
    return Request(method="GET", path="/x", query_string="", headers=headers, body=b"")


# ── Server-side timestamp + max_age ──────────────────────────────────


def test_signer_embeds_timestamp():
    """The cookie payload is a Signer token with three dot-separated parts."""
    mw = SessionMiddleware(secret_key="k" * 32)
    token = mw.encode_cookie({"a": 1})
    assert token.count(".") == 2  # payload.timestamp.sig — RFC-free shape


def test_old_token_rejected_when_past_max_age(monkeypatch):
    """A cookie signed in 2000 must not validate today regardless of cookie Max-Age."""
    mw = SessionMiddleware(secret_key="k" * 32, max_age=60)

    real_time = time.time
    fake = [real_time() - 10_000]
    monkeypatch.setattr("veloce.signing.time.time", lambda: fake[0])
    stale = mw.encode_cookie({"u": "alice"})
    monkeypatch.setattr("veloce.signing.time.time", real_time)

    assert mw.decode_cookie(stale) is None


# ── Secret rotation ──────────────────────────────────────────────────


def test_rotation_old_cookie_still_validates():
    """Cookie signed with the old secret still decodes when it's a fallback."""
    old = SessionMiddleware(secret_key="old-secret-" + "x" * 20)
    cookie_signed_old = old.encode_cookie({"user": "alice"})

    rotated = SessionMiddleware(secret_key=["new-secret-" + "y" * 20, "old-secret-" + "x" * 20])
    decoded = rotated.decode_cookie(cookie_signed_old)
    assert decoded == {"user": "alice"}


def test_rotation_new_cookie_signed_with_primary():
    """New writes use the primary secret — fallback alone can't verify them."""
    rotated = SessionMiddleware(secret_key=["new-secret-" + "y" * 20, "old-secret-" + "x" * 20])
    new_cookie = rotated.encode_cookie({"v": 2})

    just_old = SessionMiddleware(secret_key="old-secret-" + "x" * 20)
    assert just_old.decode_cookie(new_cookie) is None


def test_rotation_requires_non_empty_list():
    with pytest.raises(ValueError):
        SessionMiddleware(secret_key=[])


# ── End-to-end round trip ────────────────────────────────────────────


async def test_round_trip_via_middleware():
    """process_request reads, handler mutates, process_response signs."""
    mw = SessionMiddleware(secret_key="k" * 32)
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(mw)

    @app.get("/x")
    async def x(request: Request):
        request.session["hit"] = 1
        return {}

    resp = await app.handle_request(_req())
    set_cookie = resp.headers["Set-Cookie"]
    assert set_cookie.startswith("session=")

    # Extract just the cookie value (the bit between `session=` and the first `;`).
    cookie_val = set_cookie.split(";", 1)[0].split("=", 1)[1]
    decoded = mw.decode_cookie(cookie_val)
    assert decoded == {"hit": 1}


async def test_tampered_cookie_yields_empty_session():
    """Garbage in the cookie → empty session, not a 500."""
    mw = SessionMiddleware(secret_key="k" * 32)
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(mw)

    captured: dict = {}

    @app.get("/x")
    async def x(request: Request):
        captured["s"] = dict(request.session)
        return {}

    await app.handle_request(_req(cookie="session=not-a-real-token"))
    assert captured["s"] == {}
