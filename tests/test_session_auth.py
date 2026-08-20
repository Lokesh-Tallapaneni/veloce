"""`SessionAuth` publishes a cookie session's identity as a `Principal`.

Before this scheme existed, `request.session` and `Principal` were unrelated:
a session-logged-in user resolved to `current_principal() is None`, so a guard
written against the principal - the shape that works on the MCP door - saw an
anonymous caller over HTTP.
"""

from __future__ import annotations

from veloce import Depends, Veloce, current_principal
from veloce.middleware.sessions import SessionMiddleware
from veloce.security import SessionAuth, login_session, logout_session
from veloce.testclient import TestClient


def _app() -> Veloce:
    app = Veloce(secret_key="k", openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key="k")
    return app


def test_anonymous_request_is_rejected_by_default():
    app = _app()
    auth = SessionAuth()

    @app.get("/me")
    async def me(principal=Depends(auth)):
        return {"subject": principal.subject}

    assert TestClient(app).get("/me").status_code == 401


def test_signed_in_session_resolves_a_principal():
    app = _app()
    auth = SessionAuth()

    @app.post("/login")
    async def login(request):
        login_session(request, "ada", scopes={"items:read"})
        return {"ok": True}

    @app.get("/me")
    async def me(principal=Depends(auth)):
        return {"subject": principal.subject, "scopes": sorted(principal.scopes)}

    client = TestClient(app)
    client.post("/login")
    assert client.get("/me").json() == {"subject": "ada", "scopes": ["items:read"]}


def test_the_principal_is_published_for_the_rest_of_the_request():
    """A guard reading `current_principal()` - the same shape the MCP door
    uses - must see the session user, not an anonymous caller."""
    app = _app()
    auth = SessionAuth()

    @app.post("/login")
    async def login(request):
        login_session(request, "ada")
        return {"ok": True}

    @app.get("/who")
    async def who(_=Depends(auth)):
        principal = current_principal()
        return {"subject": None if principal is None else principal.subject}

    client = TestClient(app)
    client.post("/login")
    assert client.get("/who").json() == {"subject": "ada"}


def test_auto_error_false_resolves_anonymous_to_none():
    app = _app()
    optional = SessionAuth(auto_error=False)

    @app.get("/maybe")
    async def maybe(principal=Depends(optional)):
        return {"anonymous": principal is None}

    assert TestClient(app).get("/maybe").json() == {"anonymous": True}


def test_logout_clears_the_identity():
    app = _app()
    auth = SessionAuth()

    @app.post("/login")
    async def login(request):
        login_session(request, "ada")
        return {"ok": True}

    @app.post("/logout")
    async def logout(request):
        logout_session(request)
        return {"ok": True}

    @app.get("/me")
    async def me(principal=Depends(auth)):
        return {"subject": principal.subject}

    client = TestClient(app)
    client.post("/login")
    assert client.get("/me").status_code == 200
    client.post("/logout")
    assert client.get("/me").status_code == 401


def test_a_loader_can_reject_a_stale_subject():
    app = _app()

    def loader(request, subject):
        return None  # the user was deleted since the session was minted

    auth = SessionAuth(loader=loader)

    @app.post("/login")
    async def login(request):
        login_session(request, "ada")
        return {"ok": True}

    @app.get("/me")
    async def me(principal=Depends(auth)):
        return {"subject": principal.subject}

    client = TestClient(app)
    client.post("/login")
    assert client.get("/me").status_code == 401


def test_login_rotates_the_session_id():
    """Session fixation: an id planted before login must not survive it."""
    app = _app()
    seen: list[bool] = []

    @app.post("/login")
    async def login(request):
        seen.append(request.session.regenerate)
        login_session(request, "ada")
        seen.append(request.session.regenerate)
        return {"ok": True}

    TestClient(app).post("/login")
    assert seen[0] is False
    assert seen[1] is True
