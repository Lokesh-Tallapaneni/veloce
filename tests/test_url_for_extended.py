"""url_for control kwargs: _external/_scheme/_host/_anchor + query strings."""

from __future__ import annotations

import pytest

from veloce import Request, Router, TestClient, Veloce
from veloce import url_for as top_level_url_for


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


def test_simple_url_for():
    app = Veloce(openapi_url=None)

    @app.get("/users", name="list_users")
    async def users(request: Request):
        return []

    assert app.url_for("list_users") == "/users"


def test_url_for_with_params():
    app = Veloce(openapi_url=None)

    @app.get("/users/{user_id}/posts/{post_id}")
    async def get_post(user_id: int, post_id: int):
        return {}

    url = app.url_for("get_post", user_id="42", post_id="7")
    assert url == "/users/42/posts/7"


def test_url_for_missing_param():
    app = Veloce(openapi_url=None)

    @app.get("/users/{id}")
    async def get_user(id: int):
        return {}

    from veloce import BuildError

    with pytest.raises(BuildError):
        app.url_for("get_user")


def test_url_for_unknown_route():
    from veloce import BuildError

    app = Veloce(openapi_url=None)
    with pytest.raises(BuildError):
        app.url_for("nonexistent")


# ── A path parameter may be called `name` ────────────────────────────


def test_a_path_parameter_called_name_can_be_supplied():
    """`url_for(self, name, **path_params)` swallowed a parameter of that name.

    `url_for("download", name="x.pdf")` raised *got multiple values for
    argument 'name'* — a route could not have a segment called `name` and be
    reversed. The endpoint is positional-only, so the keyword is free.
    """
    app = Veloce(openapi_url=None)

    @app.get("/files/{name}")
    async def download(name: str):
        return {"name": name}

    assert app.url_for("download", name="report.pdf") == "/files/report.pdf"


def test_a_path_parameter_called_endpoint_can_be_supplied():
    app = Veloce(openapi_url=None)

    @app.get("/hooks/{endpoint}")
    async def hook(endpoint: str):
        return {"endpoint": endpoint}

    assert app.url_for("hook", endpoint="erp") == "/hooks/erp"


def test_the_router_reverses_a_name_segment_too():
    router = Router()

    @router.get("/files/{name}")
    async def download(name: str):
        return {"name": name}

    assert router.url_for("download", name="a.txt") == "/files/a.txt"


def test_the_request_reverses_a_name_segment_too():
    app = Veloce(openapi_url=None)

    @app.get("/files/{name}")
    async def download(name: str):
        return {"name": name}

    @app.get("/link")
    async def link(request: Request):
        return {"url": request.url_for("download", name="b.txt")}

    with TestClient(app) as client:
        assert client.get("/link").json()["url"].endswith("/files/b.txt")


# ── The top-level `url_for` helper ───────────────────────────────────


def test_the_top_level_helper_builds_a_url_inside_a_request():
    """`from veloce import url_for` did not exist.

    Templates already receive `url_for`; handlers had to reach it through
    `current_app.url_for` or hold a reference to the app.
    """
    app = Veloce(openapi_url=None)

    @app.get("/files/{name}")
    async def download(name: str):
        return {"name": name}

    @app.get("/link")
    async def link():
        return {"url": top_level_url_for("download", name="a.pdf")}

    with TestClient(app) as client:
        assert client.get("/link").json() == {"url": "/files/a.pdf"}


def test_the_top_level_helper_takes_the_endpoint_positionally():
    """So a route may still have a `{name}` or `{endpoint}` segment."""
    app = Veloce(openapi_url=None)

    @app.get("/hooks/{endpoint}")
    async def hook(endpoint: str):
        return {"endpoint": endpoint}

    @app.get("/link")
    async def link():
        return {"url": top_level_url_for("hook", endpoint="erp")}

    with TestClient(app) as client:
        assert client.get("/link").json() == {"url": "/hooks/erp"}


def test_the_top_level_helper_refuses_outside_an_application_context():
    """There is no app whose routing table could answer, so it says so."""
    with pytest.raises(RuntimeError, match="application context"):
        top_level_url_for("anything")


def test_the_top_level_helper_agrees_with_the_app_method():
    app = Veloce(openapi_url=None)

    @app.get("/files/{name}")
    async def download(name: str):
        return {"name": name}

    @app.get("/link")
    async def link():
        return {"url": top_level_url_for("download", name="b.txt")}

    with TestClient(app) as client:
        assert client.get("/link").json()["url"] == app.url_for("download", name="b.txt")
