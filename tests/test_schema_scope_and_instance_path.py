"""The schema describes the app, and `instance_path` names a definite directory.

**The documentation routes listed themselves.** `/openapi.json`, `/docs` and
`/redoc` were registered without `include_in_schema=False`, so every generated
document carried three operations for the server's own documentation endpoints:

    GET /x            operationId=x_get
    GET /openapi.json operationId=openapi_schema_get   tags=['openapi']
    GET /docs         operationId=swagger_ui_get       tags=['openapi']
    GET /redoc        operationId=redoc_ui_get         tags=['openapi']

A generated client grew a method for fetching the schema it was generated from,
and two for rendering HTML pages. `paths` describes the application's API; these
three are not part of it.

**`instance_path` accepted a relative path.** It names "a per-deployment writable
directory for config, SQLite files, uploads" — and a relative value resolves
against whatever directory the process was launched from, so the same deployment
would write its database somewhere different depending on how it was started. The
computed default is absolute; only an explicit override could be relative.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from veloce import Veloce
from veloce.testclient import TestClient


def _app(**kwargs) -> Veloce:
    app = Veloce(**kwargs)

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    return app


def _schema(app: Veloce) -> dict:
    return TestClient(app).get("/openapi.json").json()


# ── the schema describes the app, not the docs ───────────────────────


def test_the_schema_lists_only_the_apps_own_routes():
    """The defect: three documentation operations in every document."""
    assert list(_schema(_app())["paths"]) == ["/x"]


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_a_documentation_route_is_not_in_the_schema(path):
    assert path not in _schema(_app())["paths"]


def test_no_operation_carries_the_openapi_tag():
    """That tag existed only on the three routes that are now excluded."""
    schema = _schema(_app())
    tags = [op.get("tags") for ops in schema["paths"].values() for op in ops.values()]
    assert all(t is None or "openapi" not in t for t in tags)


def test_custom_documentation_paths_are_excluded_too():
    app = _app(docs_url="/documentation", redoc_url="/reference", openapi_url="/schema.json")
    schema = TestClient(app).get("/schema.json").json()
    assert list(schema["paths"]) == ["/x"]


def test_a_prefixed_app_excludes_them_too():
    app = _app(prefix="/api")
    schema = TestClient(app).get("/api/openapi.json").json()
    assert list(schema["paths"]) == ["/api/x"]


# ── the pages themselves still work ──────────────────────────────────


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_a_documentation_route_is_still_served(path):
    """Excluded from the document, not from the app."""
    assert TestClient(_app()).get(path).status_code == 200


def test_the_docs_page_still_finds_the_schema():
    """The page fetches the schema at runtime; exclusion must not break that."""
    client = TestClient(_app())
    page = client.get("/docs").text
    assert "/openapi.json" in page
    assert client.get("/openapi.json").status_code == 200


def test_the_apps_own_route_is_still_described():
    schema = _schema(_app())
    assert schema["paths"]["/x"]["get"]["operationId"] == "x_get"


def test_a_route_asking_to_be_excluded_still_is():
    """The mechanism is unchanged; it is only applied to three more routes."""
    app = Veloce()

    @app.get("/public")
    async def public() -> dict:
        return {}

    @app.get("/internal", include_in_schema=False)
    async def internal() -> dict:
        return {}

    assert list(_schema(app)["paths"]) == ["/public"]


def test_an_app_with_no_routes_has_an_empty_paths_object():
    """Previously it had three - its own documentation endpoints."""
    app = Veloce()
    assert _schema(app)["paths"] == {}


def test_the_document_is_still_valid():
    app = Veloce(validate_openapi=True)

    @app.get("/x")
    async def x() -> dict:
        return {}

    schema = app.openapi()
    assert schema["info"]["title"]
    assert "paths" in schema


# ── instance_path names a definite directory ─────────────────────────


@pytest.mark.parametrize("value", ["var/data", "instance", "./instance", "../shared", "a/b/c", ""])
def test_a_relative_instance_path_is_refused(value):
    """The defect: it resolved against the launch directory."""
    with pytest.raises(ValueError, match="instance_path must be an absolute path"):
        Veloce(openapi_url=None, instance_path=value)


def test_the_refusal_says_why():
    with pytest.raises(ValueError, match="current working directory"):
        Veloce(openapi_url=None, instance_path="var/data")


def test_the_refusal_shows_the_value():
    with pytest.raises(ValueError, match="'var/data'"):
        Veloce(openapi_url=None, instance_path="var/data")


def test_an_absolute_instance_path_is_kept():
    with tempfile.TemporaryDirectory() as directory:
        app = Veloce(openapi_url=None, instance_path=directory)
        assert app.instance_path == directory


@pytest.mark.parametrize(
    "value",
    [
        "/srv/myapp/instance",
        "/var/lib/app",
        r"\\fileserver\share",
    ],
)
def test_a_rooted_path_is_accepted_on_any_platform(value):
    """`os.path.isabs("/srv/app")` is False on Windows - no drive letter.

    Refusing on that would reject a POSIX deployment path written on a Windows
    development machine, which is the ordinary case for this project. What the
    check is actually for is a path relative to the working directory, and a
    leading separator is not that.
    """
    assert Veloce(openapi_url=None, instance_path=value).instance_path == value


def test_the_computed_default_is_absolute():
    """It always was; the override was the only way to get a relative one."""
    assert os.path.isabs(Veloce(openapi_url=None).instance_path)


def test_the_computed_default_sits_beside_the_package():
    app = Veloce(openapi_url=None)
    assert app.instance_path == os.path.join(app.package_root, "instance")


def test_no_instance_path_still_computes_one():
    app = Veloce(openapi_url=None, instance_path=None)
    assert app.instance_path.endswith("instance")


def test_the_directory_is_still_not_created():
    """Documented: the caller decides whether to `mkdir` it."""
    with tempfile.TemporaryDirectory() as directory:
        target = os.path.join(directory, "not-there")
        app = Veloce(openapi_url=None, instance_path=target)
        assert app.instance_path == target
        assert not os.path.exists(target)


def test_an_absolute_path_that_does_not_exist_is_accepted():
    """Absoluteness is the requirement, not existence."""
    target = os.path.join(tempfile.gettempdir(), "veloce-nonexistent-instance")
    app = Veloce(openapi_url=None, instance_path=target)
    assert app.instance_path == target


def test_the_app_still_serves_with_an_instance_path():
    with tempfile.TemporaryDirectory() as directory:
        app = _app(openapi_url=None, instance_path=directory)
        assert TestClient(app).get("/x").json() == {"ok": True}
