"""Regression tests for the four correctness bugs raised in the second
audit pass: A-7 (exception handler MRO cache invalidation), B-1
(blueprint bare-prefix route), B-2 (TestClient file-like multipart),
B-3 (flash backed by session, not g).
"""

from __future__ import annotations

from io import BytesIO

from veloce import Veloce
from veloce.blueprints import Blueprint
from veloce.helpers import flash, get_flashed_messages
from veloce.middleware.sessions import SessionMiddleware
from veloce.testclient import TestClient

# ── A-7 / D-6: @exception_handler decorator must invalidate the MRO cache ──


def test_exception_handler_decorator_invalidates_mro_cache():
    """A handler decorated AFTER a prior raise populates the negative
    cache must still take effect for the cached subclass type. Before
    the fix, the late registration was silently shadowed by the stale
    `_exc_handler_cache[ValueError] = None` entry."""
    app = Veloce(openapi_url=None)

    @app.get("/raise")
    def raiser():
        raise ValueError("boom")

    client = TestClient(app)
    # First request populates the cache with `None` for ValueError →
    # the dispatcher falls back to the framework 500.
    resp_no_handler = client.get("/raise")
    assert resp_no_handler.status_code == 500

    # Register a handler AFTER the cache was populated.
    @app.exception_handler(ValueError)
    def handle_value_error(request, exc):
        from veloce.http.response import JSONResponse

        return JSONResponse({"caught": str(exc)}, status_code=418)

    # The decorator must clear the cache so the new handler fires.
    resp_with_handler = client.get("/raise")
    assert resp_with_handler.status_code == 418
    assert resp_with_handler.json() == {"caught": "boom"}


def test_add_exception_handler_imperative_invalidates_mro_cache():
    """Same invariant via the imperative `add_exception_handler` shape."""
    app = Veloce(openapi_url=None)

    @app.get("/raise")
    def raiser():
        raise KeyError("missing")

    client = TestClient(app)
    assert client.get("/raise").status_code == 500

    def handle_key_error(request, exc):
        from veloce.http.response import JSONResponse

        return JSONResponse({"key": str(exc)}, status_code=422)

    app.add_exception_handler(KeyError, handle_key_error)

    resp = client.get("/raise")
    assert resp.status_code == 422


# ── B-1: blueprint root route via `@bp.get("")` ─────────────────────────────


def test_blueprint_bare_prefix_route_serves_bare_url():
    """`@bp.get("")` against a prefixed blueprint must serve the bare
    URL (`/x`), not redirect to `/x/`. The previous `_walk_routes`
    coerced an empty stripped path to `/`, which made the app
    register `/x/` and 308-redirect / 404 the bare URL."""
    bp = Blueprint("x", url_prefix="/x")

    @bp.get("")
    def root_get():
        return {"hit": "bare-prefix"}

    @bp.post("")
    def root_post():
        return {"posted": True}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)

    client = TestClient(app)
    # Bare URL — no trailing slash — must hit the handler directly.
    resp = client.get("/x")
    assert resp.status_code == 200
    assert resp.json() == {"hit": "bare-prefix"}

    # POST /x must also reach the handler without a 308 detour.
    resp_post = client.post("/x")
    assert resp_post.status_code == 200
    assert resp_post.json() == {"posted": True}


def test_blueprint_root_slash_route_still_serves_trailing_slash():
    """The trailing-slash route shape `@bp.get("/")` must keep working
    — distinct from `@bp.get("")` after the fix."""
    bp = Blueprint("y", url_prefix="/y")

    @bp.get("/")
    def root_slash():
        return {"hit": "trailing-slash"}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)

    resp = TestClient(app).get("/y/")
    assert resp.status_code == 200
    assert resp.json() == {"hit": "trailing-slash"}


# ── B-2: TestClient files= accepts file-like objects ─────────────────────────


def test_testclient_files_accepts_bytesio():
    """`files={"f": BytesIO(b"...")}` should work without a TypeError.
    Matches the `requests` / `httpx` API."""
    app = Veloce(openapi_url=None)

    @app.post("/upload")
    async def upload(request):
        form = await request.form()
        uploaded = form.get("f")
        return {
            "filename": getattr(uploaded, "filename", None),
            "content": uploaded.file.read().decode() if uploaded else None,
        }

    client = TestClient(app)
    payload = b"hello from bytesio"
    resp = client.post("/upload", files={"f": BytesIO(payload)})
    assert resp.status_code == 200
    assert resp.json()["content"] == "hello from bytesio"


def test_testclient_files_accepts_tuple_with_filelike():
    """3-tuple form with a file-like body — `(filename, BytesIO, ct)`."""
    app = Veloce(openapi_url=None)

    @app.post("/upload")
    async def upload(request):
        form = await request.form()
        uploaded = form.get("f")
        return {
            "filename": getattr(uploaded, "filename", None),
            "content": uploaded.file.read().decode() if uploaded else None,
        }

    client = TestClient(app)
    resp = client.post("/upload", files={"f": ("report.txt", BytesIO(b"contents"), "text/plain")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"] == "report.txt"
    assert body["content"] == "contents"


# ── B-3: flash() backed by session, survives POST/redirect/GET ──────────────


def test_flash_survives_redirect_via_session():
    """A flash() in a POST handler must be visible to the GET handler
    after a redirect. Before the fix, `flash` wrote to a per-request
    `g` store that the redirected GET never saw."""
    app = Veloce(openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key="x" * 32)

    @app.post("/post")
    def post_handler():
        flash("created", "success")
        from veloce.http.response import RedirectResponse

        return RedirectResponse("/show", status_code=303)

    @app.get("/show")
    def show_handler():
        return {"messages": get_flashed_messages(with_categories=True)}

    client = TestClient(app)
    # `follow_redirects=True` carries the Set-Cookie through to the
    # subsequent GET; the GET handler reads the flashes back.
    resp = client.post("/post", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.json() == {"messages": [["success", "created"]]}


def test_flash_outside_session_raises_clear_error():
    """`flash()` without SessionMiddleware must raise a descriptive
    RuntimeError — the previous silent g-backed behaviour gave no
    indication that the message would be lost on redirect.

    Asserts the message text so a future regression that swallows the
    helpful hint into a generic 500 is caught.
    """
    import pytest

    from veloce.helpers import _current_request_var
    from veloce.http.request import Request

    # Synthesise a request without a session so we can assert the
    # message directly. Going through `TestClient` would convert the
    # exception into a 500 and lose the text.
    req = Request(method="GET", path="/x", query_string="", headers={}, body=b"")
    token = _current_request_var.set(req)
    try:
        with pytest.raises(RuntimeError, match="SessionMiddleware"):
            flash("you'll never see me")
    finally:
        _current_request_var.reset(token)
