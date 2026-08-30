"""A mounted MCP endpoint reports its own security posture to `veloce check`.

The audit walks the components registered on the app — middleware, static
handlers — and asks each what it has to say. `mount_mcp` registers *routes*, so
there was no object to ask, and nothing MCP did could reach the check.

The gap was not academic. This app publishes a `purge_tenant` tool on a public
`POST /mcp` with no `auth=` and no `allowed_origins=`, so `_validate_origin`
returns immediately — and `veloce check`'s only remark was about a content
security policy:

    veloce check findings: [Finding(..., severity='info', id='csp-not-sent')]

The audit knows how to say `session-secret-key-missing`. It had nothing to say
about an unauthenticated tool-execution endpoint with no DNS-rebinding defence,
which is a strictly larger exposure.

`mount_mcp` records a posture object per network transport now, and the audit
walks those alongside everything else. `stdio` records nothing: it speaks over
the process's own pipes, where there is no port to reach and no `Origin` to
check.
"""

from __future__ import annotations

import sys

import pytest

from veloce import Veloce
from veloce.audit import run
from veloce.cli import main
from veloce.contrib.mcp._posture import MCPEndpointPosture
from veloce.contrib.mcp.auth import MCPAuth
from veloce.principal import Principal

NETWORK_TRANSPORTS = ["http", "sse"]


def _auth() -> MCPAuth:
    return MCPAuth(
        verify=lambda token: Principal(subject="s") if token == "good" else None,
        resource_server_url="https://api.example.com/mcp",
        authorization_servers=["https://auth.example.com"],
    )


def _app(**mount) -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"

    @app.mcp_tool(description="Purge a tenant")
    async def purge_tenant(tenant: str) -> dict:
        return {"purged": tenant}

    app.mount_mcp(**mount)
    return app


def _ids(app: Veloce) -> set[str]:
    return {finding.id for finding in run(app)}


def _finding(app: Veloce, finding_id: str):
    return next(f for f in run(app) if f.id == finding_id)


# ── the exposure is reported ─────────────────────────────────────────


@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
def test_an_unauthenticated_endpoint_is_reported(transport):
    """The defect: this said nothing at all."""
    assert "mcp-endpoint-unauthenticated" in _ids(_app(transport=transport))


@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
def test_an_unchecked_origin_is_reported(transport):
    assert "mcp-origin-unchecked" in _ids(_app(transport=transport))


@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
def test_both_are_warnings(transport):
    """A warning fails `veloce check`; an error would refuse a local dev boot."""
    app = _app(transport=transport)
    for finding_id in ("mcp-endpoint-unauthenticated", "mcp-origin-unchecked"):
        assert _finding(app, finding_id).severity == "warning"


@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
def test_the_finding_names_the_path(transport):
    app = _app(transport=transport, path="/agent")
    assert "/agent" in str(_finding(app, "mcp-endpoint-unauthenticated"))


@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
def test_the_finding_names_the_transport(transport):
    app = _app(transport=transport)
    assert transport in str(_finding(app, "mcp-endpoint-unauthenticated"))


@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
def test_the_fix_names_the_argument_to_pass(transport):
    app = _app(transport=transport)
    assert "auth=" in _finding(app, "mcp-endpoint-unauthenticated").fix
    assert "allowed_origins=" in _finding(app, "mcp-origin-unchecked").fix


# ── a configured endpoint reports nothing ────────────────────────────


@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
def test_an_authenticated_endpoint_is_not_reported(transport):
    app = _app(transport=transport, auth=_auth())
    assert "mcp-endpoint-unauthenticated" not in _ids(app)


@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
def test_a_checked_origin_is_not_reported(transport):
    app = _app(transport=transport, allowed_origins=["https://good.example"])
    assert "mcp-origin-unchecked" not in _ids(app)


@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
def test_a_fully_configured_endpoint_reports_neither(transport):
    app = _app(transport=transport, auth=_auth(), allowed_origins=["https://good.example"])
    ids = _ids(app)
    assert "mcp-endpoint-unauthenticated" not in ids
    assert "mcp-origin-unchecked" not in ids


def test_an_empty_origin_list_is_not_a_check():
    """An empty allowlist admits nothing, but `_validate_origin` skips on falsy."""
    assert "mcp-origin-unchecked" in _ids(_app(transport="http", allowed_origins=[]))


# ── stdio has neither concern ────────────────────────────────────────


def test_stdio_records_no_posture():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Purge a tenant")
    async def purge_tenant(tenant: str) -> dict:
        return {}

    coroutine = app.mount_mcp()
    coroutine.close()
    assert app._auditables == []


def test_stdio_reports_neither_finding():
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"

    @app.mcp_tool(description="Purge a tenant")
    async def purge_tenant(tenant: str) -> dict:
        return {}

    app.mount_mcp().close()
    ids = _ids(app)
    assert "mcp-endpoint-unauthenticated" not in ids
    assert "mcp-origin-unchecked" not in ids


# ── the plumbing ─────────────────────────────────────────────────────


def test_the_posture_is_recorded_once_per_mount():
    app = _app(transport="http")
    assert len(app._auditables) == 1
    assert isinstance(app._auditables[0], MCPEndpointPosture)


def test_two_mounts_each_report():
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"

    @app.mcp_tool(description="A tool")
    async def probe() -> dict:
        return {}

    app.mount_mcp(transport="http", path="/mcp")
    app.mount_mcp(transport="sse", path="/sse")
    assert len(app._auditables) == 2
    paths = {p.path for p in app._auditables}
    assert paths == {"/mcp", "/sse"}


def test_a_mixed_pair_reports_only_the_unconfigured_one():
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"

    @app.mcp_tool(description="A tool")
    async def probe() -> dict:
        return {}

    app.mount_mcp(transport="http", path="/secure", auth=_auth(), allowed_origins=["https://a"])
    app.mount_mcp(transport="sse", path="/open")
    reported = [str(f) for f in run(app) if f.id == "mcp-endpoint-unauthenticated"]
    assert len(reported) == 1
    assert "/open" in reported[0]


def test_the_findings_can_be_silenced():
    app = _app(transport="http")
    app.config["SILENCED_AUDIT_IDS"] = ("mcp-endpoint-unauthenticated", "mcp-origin-unchecked")
    ids = _ids(app)
    assert "mcp-endpoint-unauthenticated" not in ids
    assert "mcp-origin-unchecked" not in ids


def test_an_app_with_no_mcp_records_nothing():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert app._auditables == []


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    """Write an importable module under `tmp_path` and unimport it afterwards.

    `main(["check", ...])` imports the module, so the `sys.modules` entry
    outlives the test while `syspath_prepend` is undone - leaving the name bound
    to a torn-down directory. `monkeypatch.delitem(..., raising=False)` does not
    fix that: it records nothing to undo when the key is still absent at setup,
    which is exactly when this one is.
    """
    written: list[str] = []

    def write(name: str, source: str) -> None:
        (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
        sys.modules.pop(name, None)
        written.append(name)

    monkeypatch.syspath_prepend(str(tmp_path))
    yield write
    for name in written:
        sys.modules.pop(name, None)


# ── it fails `veloce check` ──────────────────────────────────────────


def test_veloce_check_fails_on_an_unauthenticated_endpoint(app_module):
    """The property the finding is about: the exposure reaches the exit code."""
    app_module(
        "mcp_posture_app",
        "from veloce import SecurityHeadersMiddleware, Veloce\n"
        "app = Veloce(openapi_url=None)\n"
        'app.config["SECRET_KEY"] = "k"\n'
        "app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=1,"
        " content_security_policy=\"default-src 'self'\"))\n"
        '@app.mcp_tool(description="Purge a tenant")\n'
        "async def purge_tenant(tenant: str) -> dict:\n    return {}\n"
        'app.mount_mcp(transport="http", path="/mcp")\n',
    )
    assert main(["check", "mcp_posture_app:app"]) == 1


def test_veloce_check_passes_once_it_is_configured(app_module):
    app_module(
        "mcp_secure_app",
        "from veloce import SecurityHeadersMiddleware, Veloce\n"
        "from veloce.contrib.mcp.auth import MCPAuth\n"
        "from veloce.principal import Principal\n"
        "app = Veloce(openapi_url=None)\n"
        'app.config["SECRET_KEY"] = "k"\n'
        "app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=1,"
        " content_security_policy=\"default-src 'self'\"))\n"
        '@app.mcp_tool(description="Purge a tenant")\n'
        "async def purge_tenant(tenant: str) -> dict:\n    return {}\n"
        "auth = MCPAuth(verify=lambda t: Principal(subject='s'),"
        ' resource_server_url="https://api.example.com/mcp",'
        ' authorization_servers=["https://auth.example.com"])\n'
        'app.mount_mcp(transport="http", path="/mcp", auth=auth,'
        ' allowed_origins=["https://good.example"])\n',
    )
    assert main(["check", "mcp_secure_app:app"]) == 0


# ── the endpoint still works ─────────────────────────────────────────


def test_reporting_does_not_change_what_the_endpoint_serves():
    app = _app(transport="http", path="/mcp")
    client = app.test_client()
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
        headers={"Accept": "application/json"},
    )
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "purge_tenant", "arguments": {"tenant": "acme"}},
        },
        headers={"Accept": "application/json"},
    )
    assert "acme" in response.text
