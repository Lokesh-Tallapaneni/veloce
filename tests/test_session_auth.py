"""`SessionAuth` publishes a cookie session's identity as a `Principal`.

Before this scheme existed, `request.session` and `Principal` were unrelated:
a session-logged-in user resolved to `current_principal() is None`, so a guard
written against the principal - the shape that works on the MCP door - saw an
anonymous caller over HTTP.
"""

from __future__ import annotations

import warnings

import veloce.security as security_pkg
from veloce import Depends, Veloce, current_principal
from veloce.middleware.sessions import SessionMiddleware
from veloce.security import SessionAuth, login_session, logout_session
from veloce.security.base import SecurityScheme
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


def test_a_custom_session_key_resolves_a_principal():
    """The defect: `login_session` wrote the module constants unconditionally.

    A `SessionAuth` built with a custom key then read a slot the login had
    never written, so every request 401d (or resolved anonymous) even though
    the user had just signed in.
    """
    app = _app()
    auth = SessionAuth(subject_key="uid", scopes_key="perms")

    @app.post("/login")
    async def login(request):
        login_session(request, "ada", scopes={"items:read"}, subject_key="uid", scopes_key="perms")
        return {"ok": True}

    @app.get("/me")
    async def me(principal=Depends(auth)):
        return {"subject": principal.subject, "scopes": sorted(principal.scopes)}

    client = TestClient(app)
    client.post("/login")
    assert client.get("/me").json() == {"subject": "ada", "scopes": ["items:read"]}


def test_a_custom_session_key_is_the_slot_written():
    """Stated on the session itself, so the pair above cannot pass by both
    sides agreeing on the wrong slot."""
    app = _app()
    stored: dict[str, object] = {}

    @app.post("/login")
    async def login(request):
        login_session(request, "ada", scopes={"items:read"}, subject_key="uid", scopes_key="perms")
        stored.update(request.session)
        return {"ok": True}

    TestClient(app).post("/login")
    assert stored["uid"] == "ada"
    assert stored["perms"] == ["items:read"]
    assert "_auth_subject" not in stored


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


# ── the document says the route needs a credential ───────────────────
#
# `SessionAuth` was the only shipped scheme without `openapi_scheme()`. The base
# class' own docstring spells out the consequence: "A route guarded by an
# undescribed scheme is published with no security requirement - which asserts
# the endpoint is open". A session-guarded route was published as public, so a
# generated client sent no cookie and a schema reader saw an open endpoint.
#
# OpenAPI has no session-specific type; a cookie credential is an `apiKey` read
# from `cookie`, which is how `APIKeyCookie` describes the same transport.


def _documented_app(**auth_kwargs) -> Veloce:
    app = Veloce(secret_key="k", title="T", version="1")
    app.add_middleware(SessionMiddleware, secret_key="k")
    auth = SessionAuth(**auth_kwargs)

    @app.get("/me")
    async def me(principal=Depends(auth)):
        return {"subject": principal.subject}

    return app


def test_a_session_guarded_route_declares_a_security_requirement():
    """The defect: this was `None`, publishing the route as open."""
    schema = _documented_app().openapi()
    assert schema["paths"]["/me"]["get"]["security"] == [{"SessionAuth": []}]


def test_the_session_scheme_is_published():
    schemes = _documented_app().openapi()["components"]["securitySchemes"]
    assert schemes["SessionAuth"] == {"type": "apiKey", "in": "cookie", "name": "session"}


def test_the_scheme_names_the_default_session_cookie():
    """The name must be the cookie `SessionMiddleware` actually writes, or a
    generated client sends the wrong one."""
    app = _documented_app()

    @app.post("/login")
    async def login(request):
        login_session(request, "ada")
        return {"ok": True}

    client = TestClient(app)
    sent = client.post("/login").headers["set-cookie"].split("=")[0]
    published = app.openapi()["components"]["securitySchemes"]["SessionAuth"]["name"]
    assert sent == published


def test_a_custom_cookie_name_is_published():
    schemes = _documented_app(cookie_name="sid").openapi()["components"]["securitySchemes"]
    assert schemes["SessionAuth"]["name"] == "sid"


def test_no_undescribed_scheme_warning_is_raised():
    """The framework warns rather than silently publishing an open route; that
    warning must no longer fire for this scheme."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _documented_app().openapi()
    assert [str(w.message) for w in caught if "openapi_scheme" in str(w.message)] == []


def test_every_shipped_scheme_describes_itself():
    """Stated as the property, so a new scheme cannot reintroduce the gap."""

    undescribed = []
    for name in dir(security_pkg):
        obj = getattr(security_pkg, name)
        if not isinstance(obj, type) or not issubclass(obj, SecurityScheme):
            continue
        if obj is SecurityScheme or name.startswith("_"):
            continue
        if "openapi_scheme" not in obj.__dict__ and not any(
            "openapi_scheme" in base.__dict__ for base in obj.__mro__[1:-1]
        ):
            undescribed.append(name)
    assert undescribed == [], undescribed


# ── the negative: an unguarded route stays open ──────────────────────


def test_an_unguarded_route_declares_no_security():
    """A change that attached the requirement to everything would pass the
    assertions above."""
    app = _documented_app()

    @app.get("/public")
    async def public():
        return {"ok": True}

    assert "security" not in app.openapi()["paths"]["/public"]["get"]
