"""TestClient.session_transaction — mutate the session in tests (T5)."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce
from veloce.middleware.sessions import SessionMiddleware
from veloce.testclient import TestClient

_SECRET = "k" * 32


def _make_app() -> Veloce:
    app = Veloce()
    app.add_middleware(SessionMiddleware, secret_key=_SECRET)

    @app.get("/whoami")
    async def whoami(request: Request):
        return {"user": request.session.get("user")}

    return app


def test_session_transaction_seeds_session():
    app = _make_app()
    with TestClient(app) as client:
        with client.session_transaction() as sess:
            sess["user"] = "alice"
        resp = client.get("/whoami")

    assert resp.json() == {"user": "alice"}


def test_session_transaction_reads_existing_session():
    app = _make_app()
    seen: dict = {}

    with TestClient(app) as client:
        with client.session_transaction() as sess:
            sess["user"] = "bob"
        # A second transaction sees the value the first one wrote.
        with client.session_transaction() as sess:
            seen["user"] = sess.get("user")

    assert seen == {"user": "bob"}


def test_session_transaction_requires_middleware():
    app = Veloce()

    @app.get("/x")
    async def x(request: Request):
        return {}

    with TestClient(app) as client:
        ctx = client.session_transaction()
        with pytest.raises(RuntimeError, match="SessionMiddleware"):
            ctx.__enter__()


def test_session_transaction_mutation_persists_across_requests():
    app = _make_app()
    with TestClient(app) as client:
        with client.session_transaction() as sess:
            sess["user"] = "carol"
        first = client.get("/whoami")
        second = client.get("/whoami")

    assert first.json() == {"user": "carol"}
    assert second.json() == {"user": "carol"}
