"""Something that is not middleware can still report to `veloce check`.

Middleware reports on itself through `Middleware.audit` without registering
anywhere - it is already in the stack. Anything that hardens or exposes the app
*without* being middleware had nowhere to say so: `app._auditables` was private,
and its only producer and only consumer both lived outside the class that owns
it, so the in-tree MCP integration reached it by name and nobody else could.
"""

from __future__ import annotations

import pytest

from veloce import Finding, Veloce
from veloce.audit import run as audit_run


class Exposed:
    """A component that reports a problem."""

    def audit(self, ctx):
        yield Finding(
            "telemetry endpoint accepts unauthenticated writes",
            severity="error",
            fix="pass auth=...",
            id="telemetry-unauthenticated",
        )


class Quiet:
    """A component with nothing to report."""

    def audit(self, ctx):
        return iter(())


def _app() -> Veloce:
    return Veloce(openapi_url=None)


def test_a_registered_component_reaches_the_audit() -> None:
    app = _app()
    app.register_auditable(Exposed())
    ids = {f.id for f in audit_run(app)}
    assert "telemetry-unauthenticated" in ids


def test_it_reaches_the_human_readable_rendering_too() -> None:
    app = _app()
    app.register_auditable(Exposed())
    assert any("unauthenticated writes" in line for line in app.security_audit())


def test_a_quiet_component_adds_nothing() -> None:
    app = _app()
    before = {f.id for f in audit_run(app)}
    app.register_auditable(Quiet())
    assert {f.id for f in audit_run(app)} == before


def test_it_returns_the_component_so_it_reads_as_a_decorator() -> None:
    app = _app()
    component = Quiet()
    assert app.register_auditable(component) is component


def test_registration_is_refused_once_the_app_is_serving() -> None:
    """The same setup-lock gate every other registration is under."""
    from veloce.exceptions import SetupError

    app = _app()
    app._assert_mutable()  # sanity: open before the lock trips
    app._setup_locked = True
    with pytest.raises(SetupError, match="after it has started"):
        app.register_auditable(Quiet())


def test_the_mcp_endpoint_posture_goes_through_the_public_seam() -> None:
    """The in-tree producer uses the same entry point a user has."""
    import inspect

    from veloce.contrib.mcp import _posture

    body = inspect.getsource(_posture.record_endpoint)
    assert "app.register_auditable(" in body
    assert "_auditables" not in body


def test_an_unauthenticated_mcp_mount_is_still_reported() -> None:
    """End to end: the behaviour the private list existed for, unchanged."""
    pytest.importorskip("veloce.contrib.mcp")
    app = _app()

    @app.mcp_tool(description="does a thing")
    async def do_thing() -> dict[str, str]:
        return {"ok": "yes"}

    app.mount_mcp(transport="http", path="/mcp")
    ids = {f.id for f in audit_run(app)}
    assert "mcp-endpoint-unauthenticated" in ids or "mcp-origin-unchecked" in ids
