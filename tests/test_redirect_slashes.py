"""redirect_slashes app option — trailing-slash redirect behaviour."""

from __future__ import annotations

import contextvars

from tests.conftest import make_request
from veloce import Request, Veloce


class TestRedirectSlashes:
    """The `redirect_slashes` option itself: when a redirect is issued at all."""

    async def test_trailing_slash_redirect(self):
        app = Veloce(openapi_url=None, redirect_slashes=True)

        @app.get("/users/")
        async def users(request: Request):
            return [{"id": 1}]

        resp = await app.handle_request(make_request(path="/users"))
        assert resp.status_code == 307
        assert resp.headers.get("Location") == "/users/"

    async def test_no_redirect_when_disabled(self):
        app = Veloce(openapi_url=None, redirect_slashes=False)

        @app.get("/users/")
        async def users(request: Request):
            return [{"id": 1}]

        resp = await app.handle_request(make_request(path="/users"))
        assert resp.status_code == 404


# ── the redirect target must stay on this origin ─────────────────────
#
# `Location` was built as `root_path + alt` and `alt` echoed the request path,
# so `GET //evil.com/` against a `/{username}`-shaped route answered
# `Location: //evil.com`. A `Location` starting with `//` is protocol-relative:
# the browser reads what follows as a host and leaves the origin, which is an
# open redirect from the victim's own domain - useful for phishing and for
# laundering an allowlisted `next` value that only checks "starts with /".


def _asgi_redirect(app: Veloce, path: str, root_path: str = "") -> tuple[int | None, str | None]:
    """Drive the ASGI callable and return `(status, Location)`."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [(b"host", b"victim.example")],
        "client": ("1.2.3.4", 5),
        "server": ("victim.example", 80),
        "scheme": "http",
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
        "root_path": root_path,
    }

    # Driven inside a *copied* context. A bare `coro.send(None)` runs the app in
    # this frame's context, so every contextvar it binds - `current_app`,
    # `request`, the session - leaks into the test process and outlives the
    # call. A real Task copies the context; driving by hand does not, and the
    # leak surfaced as unrelated "outside a request" tests failing later in the
    # same run.
    def _drive() -> None:
        coro = app(scope, receive, send)
        try:
            while True:
                coro.send(None)
        except StopIteration:
            pass

    contextvars.copy_context().run(_drive)
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    if start is None:
        return None, None
    location = next((v.decode() for k, v in start.get("headers", []) if k == b"location"), None)
    return start["status"], location


def _profile_app() -> Veloce:
    app = Veloce(openapi_url=None, redirect_slashes=True)

    @app.get("/{username}")
    async def profile(request: Request, username: str):
        return {"u": username}

    app._run_lifecycle("startup")
    return app


def test_a_protocol_relative_path_does_not_redirect_off_origin():
    """NEGATIVE: `//evil.com/` must not answer `Location: //evil.com`.

    The matcher refuses a path with an empty segment, so this 404s rather than
    reaching the redirect at all.
    """
    status, location = _asgi_redirect(_profile_app(), "//evil.com/")

    assert status == 404
    assert location is None


def test_a_mount_prefix_cannot_make_the_location_protocol_relative():
    """NEGATIVE: the guard is on the emitted value, not only on the path.

    `Location` is `root_path + alt`. Even with `alt` canonical, a mount whose
    `root_path` begins with `//` would produce a protocol-relative redirect.
    """
    status, location = _asgi_redirect(_profile_app(), "/alice/", root_path="//evil.com")

    assert status == 307
    assert location is not None
    assert not location.startswith("//")
    assert location == "/evil.com/alice"


def test_an_ordinary_trailing_slash_redirect_is_unchanged():
    """POSITIVE: the common case must keep its exact target."""
    status, location = _asgi_redirect(_profile_app(), "/alice/")

    assert status == 307
    assert location == "/alice"


def test_a_backslash_path_keeps_its_percent_encoded_target():
    r"""POSITIVE: `RedirectResponse` already encodes a backslash.

    `/\evil.com` cannot be read as a second slash once encoded, so the guard
    must leave it alone rather than rewriting a path it does not need to.
    """
    status, location = _asgi_redirect(_profile_app(), "/\\evil.com/")

    assert status == 307
    assert location == "/%5Cevil.com"
