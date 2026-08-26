"""flash() / get_flashed_messages() — session-backed flash messages."""

from __future__ import annotations

from veloce import Request, Veloce
from veloce.helpers import flash, get_flashed_messages
from veloce.middleware.sessions import SessionMiddleware
from veloce.testclient import TestClient


class TestFlashMessages:
    """Test flash / get_flashed_messages.

    After B-3, flashes live in the session, not in `g` — so a flash from
    a POST handler survives a redirect to the next GET. These tests use
    a real Veloce app with `SessionMiddleware` installed and drive both
    sides of the round-trip via in-process handler calls (the
    middleware sets `request._state["session"]` for `flash()` and
    `get_flashed_messages()` to consume).
    """

    def _make_app(self):

        app = Veloce(openapi_url=None)
        app.add_middleware(SessionMiddleware, secret_key="t" * 32)
        return app

    def test_flash_and_retrieve(self):

        app = self._make_app()

        @app.post("/set")
        def set_handler():
            flash("Item created", "success")
            flash("Check email", "info")
            return {"ok": True}

        @app.get("/get")
        def get_handler():
            return {"messages": get_flashed_messages()}

        client = TestClient(app)
        client.post("/set")
        # Same TestClient instance carries the session cookie forward,
        # so the GET sees the flashes the POST stored.
        resp = client.get("/get")
        assert resp.json() == {"messages": ["Item created", "Check email"]}
        # Messages are consumed: a second GET sees an empty list.
        assert client.get("/get").json() == {"messages": []}

    def test_flash_with_categories(self):

        app = self._make_app()

        @app.post("/set")
        def set_handler():
            flash("Error occurred", "error")
            flash("All good", "success")
            return {"ok": True}

        @app.get("/get")
        def get_handler():
            return {"m": get_flashed_messages(with_categories=True)}

        client = TestClient(app)
        client.post("/set")
        resp = client.get("/get")
        assert resp.json() == {"m": [["error", "Error occurred"], ["success", "All good"]]}

    def test_flash_category_filter(self):

        app = self._make_app()

        @app.post("/set")
        def set_handler():
            flash("Error 1", "error")
            flash("Success 1", "success")
            flash("Error 2", "error")
            return {"ok": True}

        @app.get("/get")
        def get_handler():
            return {"errors": get_flashed_messages(category_filter=["error"])}

        client = TestClient(app)
        client.post("/set")
        resp = client.get("/get")
        assert resp.json() == {"errors": ["Error 1", "Error 2"]}

    def test_flash_category_filter_multi(self):
        """Multiple-category filter — `info` and `warn` pass, `error` is dropped."""

        app = self._make_app()

        @app.post("/set")
        def set_handler():
            flash("i1", "info")
            flash("e1", "error")
            flash("w1", "warn")
            flash("i2", "info")
            return {"ok": True}

        @app.get("/get")
        def get_handler():
            return {"m": get_flashed_messages(category_filter=["info", "warn"])}

        client = TestClient(app)
        client.post("/set")
        resp = client.get("/get")
        assert resp.json() == {"m": ["i1", "w1", "i2"]}


def test_get_flashed_messages_with_category_filter_set():

    app = Veloce(openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="test-secret-key-32-bytes-long-ok"))
    observed = {}

    @app.get("/show")
    async def show(request: Request):
        flash("hello", "info")
        flash("be careful", "warn")
        flash("noise", "debug")
        msgs = get_flashed_messages(with_categories=True, category_filter=["info", "warn"])
        observed["msgs"] = list(msgs)
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/show")

    msgs = observed.get("msgs", [])
    categories = [c for c, _ in msgs]
    assert "info" in categories
    assert "warn" in categories
    assert "debug" not in categories


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
