"""`SessionMiddleware.encode_cookie` / `decode_cookie`.

Minting or reading a session cookie outside a request had no supported route,
so the suite reached for `middleware._signer` at thirteen sites: to build a
cookie for a request under test, to read one back off a response, and to check
that rotating a secret still accepts the old cookie.

Rebuilding the signer by hand is not equivalent. The middleware signs with a
specific salt and, on the way back in, applies a two-tier age ceiling: a token
is decoded against the longer of `max_age` and `permanent_lifetime` so a
permanent cookie is not rejected before its flag is readable, then re-validated
against the ceiling its own `_permanent` flag earns. A hand-rolled
`Signer(secret).loads(...)` skips all of that and accepts cookies the middleware
would refuse.

So the seam is not a wrapper over the signer - `decode_cookie` **is** the
request path's decode, and `process_request` calls it. The two cannot disagree,
which is the property these tests pin.
"""

from __future__ import annotations

import pytest

from veloce import Request, Veloce
from veloce.middleware.sessions import SessionMiddleware
from veloce.testclient import TestClient

SECRET = "s3cret-key-for-tests"


@pytest.fixture
def aged(monkeypatch):
    """Mint a cookie that is `seconds` old, without sleeping for it.

    `Signer` stamps and checks whole seconds (`int(time.time())`), so a real
    `sleep(1.1)` can still read as an age of 1 - the boundary the two-tier
    ceiling turns on. Moving the clock backwards for the signing call is both
    exact and instant.
    """
    import veloce.signing

    def _aged(middleware, payload, seconds):
        real = veloce.signing.time.time
        monkeypatch.setattr(veloce.signing.time, "time", lambda: real() - seconds)
        try:
            return middleware.encode_cookie(payload)
        finally:
            monkeypatch.undo()

    return _aged


def _mw(**kwargs) -> SessionMiddleware:
    return SessionMiddleware(secret_key=SECRET, **kwargs)


def _app(**kwargs) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=SECRET, **kwargs)

    @app.get("/who")
    async def who(request: Request):
        return {"user": request.session.get("user")}

    @app.post("/login")
    async def login(request: Request):
        request.session["user"] = "alice"
        return {"ok": True}

    return app


# ── round trip ───────────────────────────────────────────────────────


def test_a_session_survives_the_round_trip():
    mw = _mw()
    assert mw.decode_cookie(mw.encode_cookie({"user": "alice"})) == {"user": "alice"}


def test_an_empty_session_round_trips():
    mw = _mw()
    assert mw.decode_cookie(mw.encode_cookie({})) == {}


def test_nested_values_round_trip():
    mw = _mw()
    payload = {"user": "alice", "roles": ["a", "b"], "n": 3}
    assert mw.decode_cookie(mw.encode_cookie(payload)) == payload


def test_encode_accepts_any_mapping():
    """It is annotated `Mapping`, so a session object is as good as a dict."""
    from collections import ChainMap

    mw = _mw()
    assert mw.decode_cookie(mw.encode_cookie(ChainMap({"user": "alice"}))) == {"user": "alice"}


def test_encoding_does_not_mutate_the_input():
    mw = _mw()
    payload = {"user": "alice"}
    mw.encode_cookie(payload)
    assert payload == {"user": "alice"}


# ── rejection ────────────────────────────────────────────────────────


def test_a_tampered_value_decodes_to_none():
    mw = _mw()
    token = mw.encode_cookie({"user": "alice"})
    assert mw.decode_cookie(token[:-1] + ("x" if token[-1] != "x" else "y")) is None


def test_garbage_decodes_to_none():
    assert _mw().decode_cookie("not-a-token") is None


def test_an_empty_string_decodes_to_none():
    assert _mw().decode_cookie("") is None


def test_another_secret_decodes_to_none():
    other = SessionMiddleware(secret_key="a-completely-different-secret")
    assert other.decode_cookie(_mw().encode_cookie({"user": "alice"})) is None


def test_a_stale_token_decodes_to_none(aged):
    """Age is enforced, not just the signature."""
    mw = _mw(max_age=60)
    assert mw.decode_cookie(aged(mw, {"user": "alice"}, 600)) is None


# ── the two-tier ceiling a hand-rolled signer would miss ─────────────


def test_a_permanent_token_is_not_rejected_by_the_shorter_window(aged):
    """Decoded against the longer window so the flag is readable at all."""
    mw = _mw(max_age=60, permanent_lifetime=3600)
    token = aged(mw, {"user": "alice", "_permanent": True}, 600)
    assert mw.decode_cookie(token) == {"user": "alice", "_permanent": True}


def test_a_non_permanent_token_is_still_held_to_the_shorter_ceiling(aged):
    """The other half: the lenient window must not become the real limit.

    The same age that a permanent token survives above.
    """
    mw = _mw(max_age=60, permanent_lifetime=3600)
    assert mw.decode_cookie(aged(mw, {"user": "alice"}, 600)) is None


def test_a_stale_permanent_token_is_refused(aged):
    mw = _mw(max_age=60, permanent_lifetime=60)
    assert mw.decode_cookie(aged(mw, {"user": "alice", "_permanent": True}, 600)) is None


# ── rotation ─────────────────────────────────────────────────────────


def test_a_rotated_secret_still_reads_the_old_cookie():
    old = SessionMiddleware(secret_key="old-secret-value")
    rotated = SessionMiddleware(secret_key=["new-secret-value", "old-secret-value"])
    assert rotated.decode_cookie(old.encode_cookie({"user": "alice"})) == {"user": "alice"}


def test_the_retired_secret_cannot_read_a_new_cookie():
    old = SessionMiddleware(secret_key="old-secret-value")
    rotated = SessionMiddleware(secret_key=["new-secret-value", "old-secret-value"])
    assert old.decode_cookie(rotated.encode_cookie({"v": 2})) is None


def test_rotation_signs_with_the_first_secret():
    rotated = SessionMiddleware(secret_key=["new-secret-value", "old-secret-value"])
    only_new = SessionMiddleware(secret_key="new-secret-value")
    assert only_new.decode_cookie(rotated.encode_cookie({"v": 2})) == {"v": 2}


# ── and it agrees with the request path, which is the point ──────────


def test_a_minted_cookie_authenticates_a_request():
    """The seam is only useful if the middleware accepts what it produced."""
    app = _app()
    value = _mw().encode_cookie({"user": "alice"})
    with TestClient(app) as client:
        resp = client.get("/who", headers={"Cookie": f"session={value}"})
    assert resp.json() == {"user": "alice"}


def test_a_cookie_the_seam_refuses_is_refused_by_a_request():
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/who", headers={"Cookie": "session=forged"})
    assert resp.json() == {"user": None}


def test_the_seam_reads_the_cookie_a_request_set():
    """The other direction: decode what the middleware itself emitted."""
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/login")
    raw = resp.headers["set-cookie"]
    value = raw.split("session=", 1)[1].split(";", 1)[0]
    assert _mw().decode_cookie(value) == {"user": "alice"}


@pytest.mark.parametrize("permanent", [True, False])
def test_the_seam_and_a_request_agree_on_a_stale_token(permanent, aged):
    """Both doors apply the same ceiling, including the `_permanent` tier."""
    app = _app(max_age=60, permanent_lifetime=60)
    mw = _mw(max_age=60, permanent_lifetime=60)
    payload = {"user": "alice"}
    if permanent:
        payload["_permanent"] = True
    value = aged(mw, payload, 600)
    assert mw.decode_cookie(value) is None
    with TestClient(app) as client:
        assert client.get("/who", headers={"Cookie": f"session={value}"}).json() == {"user": None}
