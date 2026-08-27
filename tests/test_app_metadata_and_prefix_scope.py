"""`title`/`version` are required strings, and `prefix` has a stated boundary.

**A non-string `title` produced an invalid document and a 500.** Both fields are
REQUIRED strings in an OpenAPI document (3.1 §4.8.2) and both are interpolated
into the two HTML pages, where `html.escape(None)` raised:

    Veloce(title=None, version=None, validate_openapi=True)
      info      -> {'title': None, 'version': None}
      GET /docs -> 500

Two things were wrong. The value was accepted at construction and only failed
much later, on a request; and `validate_openapi=True` — explicitly asked for —
checked `$ref`s and container shapes but never the two fields the specification
marks required, so it passed a document it existed to reject.

**`prefix` does not apply to `app.mount()`.** It applies to routes the app
registers, which includes the documentation and MCP routes because those are
registered as routes. A mount places another application at the path it is given.
That follows from the mechanism and is fine — but neither docstring said so, and
the asymmetry with `mount_mcp` (which *is* prefixed) is surprising enough to
pin down.

**`openapi_url=""` disables the two pages with it.** Both read that document, so
there is nothing for them to show. Defensible, and previously undocumented.
"""

from __future__ import annotations

import inspect

import pytest

from veloce import Veloce
from veloce.app.mounting import MountingMixin
from veloce.contrib.openapi import _validate_document
from veloce.testclient import TestClient

# ── title and version are refused at construction ────────────────────


@pytest.mark.parametrize("value", [None, 123, "", b"bytes", 1.5, ["list"], object()])
@pytest.mark.parametrize("field", ["title", "version"])
def test_a_non_string_is_refused(field, value):
    """The defect: accepted, then a 500 on the first look at /docs."""
    with pytest.raises(ValueError, match=f"{field} must be a non-empty string"):
        Veloce(openapi_url=None, **{field: value})


@pytest.mark.parametrize("field", ["title", "version"])
def test_the_refusal_says_why_it_matters(field):
    with pytest.raises(ValueError, match="/docs"):
        Veloce(openapi_url=None, **{field: None})


@pytest.mark.parametrize("field", ["title", "version"])
def test_the_refusal_shows_the_value(field):
    with pytest.raises(ValueError, match="got None"):
        Veloce(openapi_url=None, **{field: None})


def test_a_valid_title_and_version_are_kept():
    app = Veloce(openapi_url=None, title="My API", version="2.0.0")
    assert app.title == "My API"
    assert app.version == "2.0.0"


def test_the_defaults_are_valid():
    app = Veloce(openapi_url=None)
    assert isinstance(app.title, str) and app.title
    assert isinstance(app.version, str) and app.version


def test_the_document_carries_them():
    app = Veloce(title="My API", version="2.0.0")

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert app.openapi()["info"] == {"title": "My API", "version": "2.0.0"}


def test_the_docs_page_renders_with_them():
    app = Veloce(title="My API", version="2.0.0")

    @app.get("/x")
    async def x() -> dict:
        return {}

    client = TestClient(app)
    assert client.get("/docs").status_code == 200
    assert "My API" in client.get("/docs").text
    assert client.get("/redoc").status_code == 200


def test_a_title_needing_escaping_is_still_escaped():
    app = Veloce(title="A & B <script>", version="1.0")

    @app.get("/x")
    async def x() -> dict:
        return {}

    page = TestClient(app).get("/docs").text
    assert "A &amp; B &lt;script&gt;" in page
    assert "<script>alert" not in page


# ── the validator checks the required fields ─────────────────────────


@pytest.mark.parametrize("field", ["title", "version"])
def test_the_validator_rejects_a_missing_required_field(field):
    """The gap: `validate_openapi=True` passed a document it should reject."""

    document = {"info": {"title": "t", "version": "1"}, "paths": {}}
    del document["info"][field]
    with pytest.raises(ValueError, match=f"info.{field}"):
        _validate_document(document)


@pytest.mark.parametrize("value", [None, 123, ""])
@pytest.mark.parametrize("field", ["title", "version"])
def test_the_validator_rejects_a_non_string_required_field(field, value):

    document = {"info": {"title": "t", "version": "1"}, "paths": {}}
    document["info"][field] = value
    with pytest.raises(ValueError, match=f"info.{field}"):
        _validate_document(document)


def test_the_validator_rejects_a_missing_info_object():

    with pytest.raises(ValueError, match="`info` must be an object"):
        _validate_document({"paths": {}})


def test_the_validator_accepts_a_well_formed_document():

    _validate_document({"info": {"title": "t", "version": "1"}, "paths": {}})


def test_the_validator_still_catches_a_dangling_ref():
    """The checks it already had must survive the new ones."""

    document = {
        "info": {"title": "t", "version": "1"},
        "paths": {"/x": {"get": {"responses": {"200": {"$ref": "#/components/schemas/Gone"}}}}},
    }
    with pytest.raises(ValueError, match="unresolved schema"):
        _validate_document(document)


def test_a_real_app_passes_validation():
    app = Veloce(validate_openapi=True)

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    assert app.openapi()["info"]["title"]


# ── prefix applies to routes, not to mounts ──────────────────────────


def _mounted_app() -> Veloce:
    child = Veloce(openapi_url=None)

    @child.get("/ping")
    async def ping() -> dict:
        return {"from": "child"}

    app = Veloce(openapi_url=None, prefix="/api")

    @app.get("/x")
    async def x() -> dict:
        return {"from": "parent"}

    app.mount("/sub", child)
    return app


def test_a_route_is_prefixed():
    assert TestClient(_mounted_app()).get("/api/x").json() == {"from": "parent"}


def test_a_mount_is_not_prefixed():
    """Documented: `mount` places an app at the path it is given."""
    assert TestClient(_mounted_app()).get("/sub/ping").json() == {"from": "child"}


def test_the_prefixed_mount_path_is_not_served():
    assert TestClient(_mounted_app()).get("/api/sub/ping").status_code == 404


def test_writing_the_full_mount_path_works():
    """The stated remedy in the docstring."""
    child = Veloce(openapi_url=None)

    @child.get("/ping")
    async def ping() -> dict:
        return {"from": "child"}

    app = Veloce(openapi_url=None, prefix="/api")
    app.mount("/api/sub", child)
    assert TestClient(app).get("/api/sub/ping").json() == {"from": "child"}


def test_the_documentation_routes_are_prefixed():
    """They are registered as routes, which is why they differ from a mount."""
    app = Veloce(prefix="/api")

    @app.get("/x")
    async def x() -> dict:
        return {}

    client = TestClient(app)
    assert client.get("/api/docs").status_code == 200
    assert client.get("/docs").status_code == 404


def test_an_mcp_mount_is_prefixed():
    """`mount_mcp` registers routes, so it takes the prefix; `mount` does not."""
    app = Veloce(openapi_url=None, prefix="/api")

    @app.mcp_tool(description="A tool")
    async def probe() -> dict:
        return {}

    app.mount_mcp(transport="http", path="/mcp")
    client = TestClient(app)

    # A real handshake, so this proves the MCP endpoint is mounted here rather
    # than that *something* answered. The previous assertion was a bare
    # `assert ....status_code`, which every HTTP status satisfies - it could not
    # have failed even if the mount had landed nowhere.
    handshake = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"},
        },
    }
    prefixed = client.post("/api/mcp", json=handshake, headers={"Accept": "application/json"})
    assert prefixed.status_code == 200
    assert prefixed.json()["result"]["serverInfo"]

    assert client.post("/mcp", json={}, headers={"Accept": "application/json"}).status_code == 404


# ── openapi_url disabled takes the pages with it ─────────────────────


@pytest.mark.parametrize("value", [None, ""])
def test_disabling_the_schema_disables_both_pages(value):
    app = Veloce(openapi_url=value)

    @app.get("/x")
    async def x() -> dict:
        return {}

    client = TestClient(app)
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_the_app_still_serves_its_own_routes_without_a_schema():
    app = Veloce(openapi_url="")

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    assert TestClient(app).get("/x").json() == {"ok": True}


def test_the_docstrings_state_the_boundaries():
    """These were the gap; a future edit that drops them fails here."""
    assert "does not apply here" in inspect.getdoc(MountingMixin.mount)
    source = inspect.getsource(Veloce.__init__)
    assert "does not apply to `app.mount()`" in source
    assert "both pages read this document" in source
