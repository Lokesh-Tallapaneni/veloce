"""A value has one definition, and the definition is the one that is used.

Two of these had already drifted — the copies disagreed, so the behaviour was
wrong, not merely fragile:

* `app.openapi_version` is documented as "the spec version string emitted in the
  document". Nothing read it. Setting it changed the attribute and the document
  still said `3.1.0`.
* The default application title was spelled three times, and one of the three
  said something different: `getattr(app, "title", "Veloce API")` in the OpenAPI
  builder against `"Veloce"` in the constructor and the MCP server. One app named
  itself two things across its two doors.

The rest were still equal, and are the same latent hazard the two above already
became. Each is now read from the module that owns it:

* `413` from `veloce.status`, not a local `_HTTP_413_CONTENT_TOO_LARGE = 413`
  whose comment claimed importing `status` would be costly — `status.py` imports
  nothing at all, `http/response.py` already imports it, and `_body.py` already
  pulled it in transitively through `veloce.exceptions`.
* `"http.request"` / `"http.disconnect"` from `_protocol_constants`.
* The five `MSG_LABEL_COOKIE_*` labels, which had been extracted into
  `_constants` and then never wired to the call sites they were extracted from —
  they were the only unused names in that module.
* The two MCP `_meta` client keys, duplicated with a comment claiming `server`
  imports `session`; it does, but only under `TYPE_CHECKING`.
* `"Not authenticated"`, `"field required"`, the SSE retry hint, and the
  `2025-06-18` protocol revision.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

import veloce.status
from veloce import Depends, SessionMiddleware, Veloce
from veloce._protocol_constants import ASGI_EVENT_HTTP_DISCONNECT, ASGI_EVENT_HTTP_REQUEST
from veloce.contrib.mcp import _helpers
from veloce.contrib.mcp.server import (
    _SUPPORTED_PROTOCOL_VERSIONS,
    PRIOR_PROTOCOL_VERSION,
    SERVED_PROTOCOL_VERSIONS,
    MCPServer,
)
from veloce.contrib.mcp.session import META_CLIENT_CAPABILITIES, META_CLIENT_INFO
from veloce.http._body import too_large_payload
from veloce.http.cookies import dump_cookie
from veloce.security.session import SessionAuth
from veloce.testclient import TestClient

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce"


def _app(**kwargs) -> Veloce:
    app = Veloce(**kwargs)

    @app.get("/x")
    async def x() -> dict:
        return {}

    return app


# ── drifted: openapi_version was inert ───────────────────────────────


def test_the_default_openapi_version_is_emitted():
    assert _app(openapi_url=None).openapi()["openapi"] == "3.1.0"


def test_setting_openapi_version_changes_the_document():
    """The defect: the attribute changed and the document did not."""
    app = _app(openapi_url=None)
    app.openapi_version = "3.0.3"
    assert app.openapi()["openapi"] == "3.0.3"


def test_the_attribute_and_the_document_agree():
    app = _app(openapi_url=None)
    for version in ("3.1.0", "3.0.3", "3.0.0"):
        app.openapi_version = version
        app.openapi_schema = None
        assert app.openapi()["openapi"] == app.openapi_version


# ── drifted: the title default disagreed with itself ─────────────────


def test_the_two_doors_report_the_same_title():
    """The defect: the OpenAPI builder said "Veloce API", the MCP server "Veloce"."""

    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="A tool")
    async def probe() -> dict:
        return {}

    assert MCPServer(app).server_name == app.title


def test_the_two_doors_report_the_same_version():

    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="A tool")
    async def probe() -> dict:
        return {}

    assert MCPServer(app).server_version == app.version


def test_a_custom_title_reaches_both_doors():

    app = Veloce(openapi_url=None, title="Orders", version="4.2")

    @app.mcp_tool(description="A tool")
    async def probe() -> dict:
        return {}

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert app.openapi()["info"] == {"title": "Orders", "version": "4.2"}
    server = MCPServer(app)
    assert server.server_name == "Orders"
    assert server.server_version == "4.2"


@pytest.mark.parametrize(
    ("path", "literal"),
    [
        ("contrib/openapi.py", '"Veloce API"'),
        ("contrib/openapi.py", 'getattr(app, "title"'),
        ("contrib/mcp/server.py", 'getattr(app, "title"'),
        ("contrib/mcp/server.py", 'getattr(app, "version"'),
    ],
)
def test_no_second_copy_of_the_metadata_default(path, literal):
    """The constructor guarantees both, so a fallback is a duplicated default.

    Comment lines are stripped first - the removed literal is quoted in the
    comment that records why it went.
    """
    source = (SRC / path).read_text(encoding="utf-8")
    code = chr(10).join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert literal not in code


# ── the status code has one definition ───────────────────────────────


def test_the_body_module_uses_the_canonical_status():
    source = (SRC / "http" / "_body.py").read_text(encoding="utf-8")
    assert "_HTTP_413_CONTENT_TOO_LARGE" not in source
    assert "HTTP_413_CONTENT_TOO_LARGE" in source


def test_the_payload_still_carries_413():

    assert too_large_payload(10)["status_code"] == 413


def test_the_canonical_status_is_importable_from_http():
    """The removed comment claimed this would be costly; it is not."""

    assert veloce.status.HTTP_413_CONTENT_TOO_LARGE == 413


def test_the_status_module_imports_nothing():
    """Which is why no module can create a cycle by importing it."""
    tree = ast.parse((SRC / "status.py").read_text(encoding="utf-8"))
    imports = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and getattr(node, "module", None) != "__future__"
    ]
    assert imports == []


# ── the ASGI event types have one definition ─────────────────────────


def test_the_body_module_uses_the_canonical_event_types():
    source = (SRC / "http" / "_body.py").read_text(encoding="utf-8")
    assert '_ASGI_HTTP_REQUEST = "http.request"' not in source
    assert "ASGI_EVENT_HTTP_REQUEST" in source


def test_the_event_types_still_match_the_wire():

    assert ASGI_EVENT_HTTP_REQUEST == "http.request"
    assert ASGI_EVENT_HTTP_DISCONNECT == "http.disconnect"


# ── the cookie labels are used where they were extracted from ────────


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MSG_LABEL_COOKIE_NAME", "cookie name"),
        ("MSG_LABEL_COOKIE_VALUE", "cookie value"),
        ("MSG_LABEL_COOKIE_PATH", "cookie path"),
        ("MSG_LABEL_COOKIE_DOMAIN", "cookie domain"),
        ("MSG_LABEL_COOKIE_SAMESITE", "cookie samesite"),
    ],
)
def test_a_cookie_label_is_referenced_not_respelled(name, value):
    source = (SRC / "http" / "cookies.py").read_text(encoding="utf-8")
    assert name in source
    assert f'"{value}"' not in source


@pytest.mark.parametrize(
    ("kwargs", "label"),
    [
        ({"key": "a\rb", "value": "v"}, "cookie name"),
        ({"key": "k", "value": "a\rb"}, "cookie value"),
        ({"key": "k", "value": "v", "path": "a\rb"}, "cookie path"),
        ({"key": "k", "value": "v", "domain": "a\rb"}, "cookie domain"),
    ],
)
def test_the_label_still_reaches_the_error_message(kwargs, label):
    """Referencing the constant must not change what a user is told."""

    with pytest.raises(ValueError, match=label):
        dump_cookie(**kwargs)


def test_a_valid_cookie_is_still_rendered():

    assert dump_cookie("k", "v", path="/").startswith("k=v")


# ── the remaining single-value duplicates ────────────────────────────


def test_the_meta_keys_are_imported_not_respelled():
    source = (SRC / "contrib" / "mcp" / "session.py").read_text(encoding="utf-8")
    assert '"io.modelcontextprotocol/clientInfo"' not in source
    assert "META_CLIENT_INFO" in source


def test_the_meta_keys_still_have_their_wire_values():

    assert META_CLIENT_INFO == "io.modelcontextprotocol/clientInfo"
    assert META_CLIENT_CAPABILITIES == "io.modelcontextprotocol/clientCapabilities"


def test_the_session_module_does_not_import_the_dispatch_core():
    """The five `_meta` keys live in `_helpers`, which both leaves may import.

    `session.py` is a 148-line per-connection state object; `server.py` is the
    1,675-line dispatch core, and it imports `session` only under
    `TYPE_CHECKING`. Importing `server` from `session` for two string constants
    made the one runtime edge between them run leaf to core.
    """
    source = (SRC / "contrib" / "mcp" / "session.py").read_text(encoding="utf-8")
    assert "from veloce.contrib.mcp.server import" not in source, (
        "session imports the dispatch core at runtime again"
    )
    assert "from veloce.contrib.mcp._helpers import" in source


def test_every_meta_key_has_one_definition():
    """All five, not just the two `session` reads."""

    keys = {
        "META_PROTOCOL_VERSION": "io.modelcontextprotocol/protocolVersion",
        "META_CLIENT_INFO": "io.modelcontextprotocol/clientInfo",
        "META_CLIENT_CAPABILITIES": "io.modelcontextprotocol/clientCapabilities",
        "META_LOG_LEVEL": "io.modelcontextprotocol/logLevel",
        "META_SERVER_INFO": "io.modelcontextprotocol/serverInfo",
    }
    for name, wire in keys.items():
        assert getattr(_helpers, name) == wire

    defining = [
        path.name
        for path in (SRC / "contrib" / "mcp").rglob("*.py")
        if any(f'= "{wire}"' in path.read_text(encoding="utf-8") for wire in keys.values())
    ]
    assert defining == ["_helpers.py"], f"the wire values are spelled in {defining}"


def test_the_session_module_imports_on_its_own():
    """It must not need the dispatch core to be importable."""
    result = subprocess.run(
        [sys.executable, "-c", "import veloce.contrib.mcp.session"],
        capture_output=True,
        text=True,
        cwd=SRC.parents[1],
        env={**__import__("os").environ, "PYTHONPATH": "src"},
    )
    assert result.returncode == 0, result.stderr


def test_the_not_authenticated_message_is_referenced():
    source = (SRC / "security" / "session.py").read_text(encoding="utf-8")
    assert '"Not authenticated"' not in source
    assert "MSG_NOT_AUTHENTICATED" in source


def test_the_field_required_message_is_referenced():
    source = (SRC / "dependency.py").read_text(encoding="utf-8")
    assert '"msg": "field required"' not in source
    assert "MSG_FIELD_REQUIRED" in source


def test_the_sse_retry_hint_has_one_definition():
    sse = (SRC / "contrib" / "mcp" / "transports" / "sse.py").read_text(encoding="utf-8")
    http = (SRC / "contrib" / "mcp" / "transports" / "http.py").read_text(encoding="utf-8")
    assert "_SSE_RETRY_MS = 3000" not in sse
    assert "_SSE_RETRY_MS = 3000" in http
    assert "_SSE_RETRY_MS" in sse


def test_the_prior_protocol_revision_has_one_definition():
    source = (SRC / "contrib" / "mcp" / "server.py").read_text(encoding="utf-8")
    assert source.count('"2025-06-18"') == 1


def test_the_prior_revision_is_still_served():

    assert PRIOR_PROTOCOL_VERSION == "2025-06-18"
    assert PRIOR_PROTOCOL_VERSION in _SUPPORTED_PROTOCOL_VERSIONS
    assert PRIOR_PROTOCOL_VERSION in SERVED_PROTOCOL_VERSIONS


# ── the behaviour those constants drive is unchanged ─────────────────


def test_a_handshake_client_can_still_negotiate_the_prior_revision():

    app = Veloce(title="t", version="1", openapi_url=None)

    @app.mcp_tool(description="A tool")
    async def probe() -> dict:
        return {}

    app.mount_mcp(transport="http", path="/mcp")
    response = TestClient(app).post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
        headers={"Accept": "application/json"},
    )
    assert response.json()["result"]["protocolVersion"] == "2025-06-18"


def test_a_missing_credential_still_says_not_authenticated():
    """End to end: a session-guarded route refuses with the shared message."""

    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(SessionMiddleware(secret_key="k" * 32))

    @app.get("/private", dependencies=[Depends(SessionAuth())])
    async def private() -> dict:
        return {}

    assert TestClient(app).get("/private").json()["detail"] == "Not authenticated"
