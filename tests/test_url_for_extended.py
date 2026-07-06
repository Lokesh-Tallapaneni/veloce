"""url_for control kwargs: _external/_scheme/_host/_anchor + query strings."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce


def _make_app() -> Veloce:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/users/{id}", name="user")
    async def user(id: str):
        return {}

    @app.get("/list", name="list")
    async def listing():
        return {}

    return app


# ── Path params + extras → query string ──────────────────────────────


def test_extra_kwargs_become_query_string():
    app = _make_app()
    url = app.url_for("user", id="42", page=2, sort="name")
    assert url.startswith("/users/42?")
    # urlencode order matches kwargs insertion order.
    assert "page=2" in url
    assert "sort=name" in url


def test_repeated_query_value_via_list():
    app = _make_app()
    url = app.url_for("list", tag=["a", "b"])
    assert url == "/list?tag=a&tag=b"


# ── _anchor ──────────────────────────────────────────────────────────


def test_anchor_appended():
    app = _make_app()
    url = app.url_for("user", id="42", _anchor="info")
    assert url == "/users/42#info"


def test_anchor_after_query_string():
    app = _make_app()
    url = app.url_for("user", id="42", q="hi", _anchor="info")
    assert url == "/users/42?q=hi#info"


# ── _external / _scheme / _host ──────────────────────────────────────


def test_external_default_localhost():
    app = _make_app()
    url = app.url_for("user", id="42", _external=True)
    assert url == "http://localhost/users/42"


def test_external_uses_server_name_config():
    app = _make_app()
    app.config["SERVER_NAME"] = "api.example.com"
    url = app.url_for("user", id="42", _external=True)
    assert url == "http://api.example.com/users/42"


def test_external_scheme_override():
    app = _make_app()
    app.config["SERVER_NAME"] = "api.example.com"
    url = app.url_for("user", id="42", _scheme="https")
    assert url == "https://api.example.com/users/42"


def test_external_host_override():
    app = _make_app()
    url = app.url_for("user", id="42", _host="b.example.com")
    assert url == "http://b.example.com/users/42"


def test_preferred_url_scheme_config():
    app = _make_app()
    app.config["SERVER_NAME"] = "api.example.com"
    app.config["PREFERRED_URL_SCHEME"] = "https"
    url = app.url_for("user", id="42", _external=True)
    assert url == "https://api.example.com/users/42"


# ── Path-param validation still works ────────────────────────────────


def test_missing_path_param_still_raises():
    from veloce import BuildError

    app = _make_app()
    with pytest.raises(BuildError):
        app.url_for("user", _external=True)


class TestUrlFor:
    def test_simple_url_for(self):
        app = Veloce(openapi_url=None)

        @app.get("/users", name="list_users")
        async def users(request: Request):
            return []

        assert app.url_for("list_users") == "/users"

    def test_url_for_with_params(self):
        app = Veloce(openapi_url=None)

        @app.get("/users/{user_id}/posts/{post_id}")
        async def get_post(user_id: int, post_id: int):
            return {}

        url = app.url_for("get_post", user_id="42", post_id="7")
        assert url == "/users/42/posts/7"

    def test_url_for_missing_param(self):
        app = Veloce(openapi_url=None)

        @app.get("/users/{id}")
        async def get_user(id: int):
            return {}

        from veloce import BuildError

        with pytest.raises(BuildError):
            app.url_for("get_user")

    def test_url_for_unknown_route(self):
        from veloce import BuildError

        app = Veloce(openapi_url=None)
        with pytest.raises(BuildError):
            app.url_for("nonexistent")
