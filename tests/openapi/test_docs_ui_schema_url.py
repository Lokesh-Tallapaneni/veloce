"""Both documentation pages point at the URL the schema is actually served from.

The schema and the two interactive pages are registered with `app.get(...)`,
which prepends the router's `prefix`. The HTML templates interpolated the bare
`openapi_url`. So on a prefixed app the pages loaded, fetched a URL that was not
there, and rendered empty:

    GET /api/v1/docs      -> 200
    the page fetches      -> /openapi.json
    that URL              -> 404
    the schema really is  -> /api/v1/openapi.json

Naming the full path was not a workaround, because the prefix is applied to it
too: `Veloce(prefix="/api/v1", openapi_url="/api/v1/openapi.json")` serves the
schema at `/api/v1/api/v1/openapi.json`. Swagger could be rescued through
`swagger_ui_parameters={"url": ...}`; ReDoc had no escape hatch at all, since its
template takes no user parameters.

No test in the suite constructed `Veloce(prefix=...)`, which is how it survived.

The pages now name `<root_path><prefix><openapi_url>`. `root_path` is added per
request rather than at registration, because it describes how the app is mounted
at runtime, not how its routes were declared.
"""

from __future__ import annotations

import re

import pytest

from veloce import Veloce

#: (constructor kwargs, the path the docs pages should name)
LAYOUTS = [
    ({}, "/openapi.json"),
    ({"prefix": "/api/v1"}, "/api/v1/openapi.json"),
    ({"prefix": "/api"}, "/api/openapi.json"),
    ({"root_path": "/mounted"}, "/mounted/openapi.json"),
    ({"prefix": "/api/v1", "root_path": "/mounted"}, "/mounted/api/v1/openapi.json"),
    ({"openapi_url": "/schema.json"}, "/schema.json"),
    ({"prefix": "/api", "openapi_url": "/schema.json"}, "/api/schema.json"),
]


def _app(**kwargs) -> Veloce:
    app = Veloce(**kwargs)

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    return app


def _swagger_url(html: str) -> str:
    found = re.findall(r'url: "([^"]+)"', html)
    assert found, "the Swagger page names no schema URL"
    return found[0]


def _redoc_url(html: str) -> str:
    found = re.findall(r"spec-url='([^']+)'", html)
    assert found, "the ReDoc page names no schema URL"
    return found[0]


# ── the page names the served path ───────────────────────────────────


@pytest.mark.parametrize(("kwargs", "expected"), LAYOUTS)
def test_swagger_names_the_served_schema_path(kwargs, expected):
    """The defect: it named the bare `openapi_url` whatever the prefix was."""
    app = _app(**kwargs)
    prefix = kwargs.get("prefix", "")
    assert _swagger_url(app.test_client().get(f"{prefix}/docs").text) == expected


@pytest.mark.parametrize(("kwargs", "expected"), LAYOUTS)
def test_redoc_names_the_served_schema_path(kwargs, expected):
    """ReDoc had no escape hatch, so this was the only way to fix it."""
    app = _app(**kwargs)
    prefix = kwargs.get("prefix", "")
    assert _redoc_url(app.test_client().get(f"{prefix}/redoc").text) == expected


@pytest.mark.parametrize(("kwargs", "expected"), LAYOUTS)
def test_the_named_path_actually_serves_the_schema(kwargs, expected):
    """The property that matters: following the page's own URL works.

    `root_path` is stripped by the server before routing, so it is removed here
    the same way to ask the app what it would answer.
    """
    app = _app(**kwargs)
    root = kwargs.get("root_path", "")
    routable = expected[len(root) :] if root else expected
    response = app.test_client().get(routable)
    assert response.status_code == 200
    assert response.json()["openapi"].startswith("3.")


@pytest.mark.parametrize(("kwargs", "expected"), LAYOUTS)
def test_both_pages_agree(kwargs, expected):
    """One schema, one URL - they are rendered by different templates."""
    client = _app(**kwargs).test_client()
    prefix = kwargs.get("prefix", "")
    assert _swagger_url(client.get(f"{prefix}/docs").text) == _redoc_url(
        client.get(f"{prefix}/redoc").text
    )


# ── the pages still load ─────────────────────────────────────────────


@pytest.mark.parametrize(("kwargs", "_expected"), LAYOUTS)
def test_both_pages_are_served(kwargs, _expected):
    client = _app(**kwargs).test_client()
    prefix = kwargs.get("prefix", "")
    assert client.get(f"{prefix}/docs").status_code == 200
    assert client.get(f"{prefix}/redoc").status_code == 200


def test_a_prefixed_app_serves_its_routes_too():
    """Nothing about routing changed; the prefix still applies to the app."""
    client = _app(prefix="/api/v1").test_client()
    assert client.get("/api/v1/x").json() == {"ok": True}
    assert client.get("/x").status_code == 404


def test_custom_doc_urls_are_prefixed_too():
    app = Veloce(prefix="/api", docs_url="/documentation", redoc_url="/reference")

    @app.get("/x")
    async def x() -> dict:
        return {}

    client = app.test_client()
    assert client.get("/api/documentation").status_code == 200
    assert _swagger_url(client.get("/api/documentation").text) == "/api/openapi.json"
    assert _redoc_url(client.get("/api/reference").text) == "/api/openapi.json"


# ── the escape hatch still overrides ─────────────────────────────────


def test_swagger_ui_parameters_can_still_override_the_url():
    """It was the only rescue before; it must keep winning over the default."""
    app = Veloce(prefix="/api", swagger_ui_parameters={"url": "/elsewhere.json"})

    @app.get("/x")
    async def x() -> dict:
        return {}

    page = app.test_client().get("/api/docs").text
    assert "/elsewhere.json" in page


# ── escaping is not lost ─────────────────────────────────────────────


def test_the_url_is_html_escaped():
    """It is interpolated into HTML; a prefix is user-supplied text."""
    app = Veloce(prefix="/a&b")

    @app.get("/x")
    async def x() -> dict:
        return {}

    page = app.test_client().get("/a&b/docs").text
    assert "/a&amp;b/openapi.json" in page
    assert 'url: "/a&b/openapi.json"' not in page


def test_a_quote_in_the_prefix_cannot_break_out_of_the_attribute():
    app = Veloce(prefix="/a'b")

    @app.get("/x")
    async def x() -> dict:
        return {}

    page = app.test_client().get("/a'b/redoc").text
    assert "spec-url='/a&#x27;b/openapi.json'" in page


# ── an app with the schema disabled ──────────────────────────────────


def test_no_openapi_url_registers_no_pages():
    app = Veloce(openapi_url=None, prefix="/api")

    @app.get("/x")
    async def x() -> dict:
        return {}

    client = app.test_client()
    assert client.get("/api/docs").status_code == 404
    assert client.get("/api/redoc").status_code == 404


def test_docs_can_be_disabled_while_the_schema_stays():
    app = Veloce(prefix="/api", docs_url=None, redoc_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {}

    client = app.test_client()
    assert client.get("/api/docs").status_code == 404
    assert client.get("/api/openapi.json").status_code == 200
