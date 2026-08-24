"""Severity, silencing, and the phase a check runs in.

One hook answers two questions that used to need two. A misconfiguration is an
`error` and refuses the boot; a posture finding is a `warning` and is reported.
The caller decides which, so the middleware writes one method.

A check reading the route table declares it, because `veloce check` audits an
app it never starts - routes registered during startup do not exist yet, and
asking then would report a live route as missing.
"""

from __future__ import annotations

from typing import Any

import pytest

from veloce import (
    AuditContext,
    AuditFailed,
    Finding,
    Middleware,
    RateLimitMiddleware,
    TestClient,
    Veloce,
)
from veloce.audit import run
from veloce.ratelimit import FixedWindow


def _app(*middleware: Middleware, **kw: Any) -> Veloce:
    app = Veloce(openapi_url=None, **kw)
    app.config["SECRET_KEY"] = "k"
    app.use_secure_defaults()
    for mw in middleware:
        app.add_middleware(mw)
    return app


# ── severity ─────────────────────────────────────────────────────────


class Fatal(Middleware):
    def audit(self, ctx: AuditContext) -> Any:
        return (Finding("broken.", "error", fix="fix it", id="fatal"),)


class Noisy(Middleware):
    def audit(self, ctx: AuditContext) -> Any:
        return (Finding("worth knowing.", "info", id="fyi"),)


def test_severity_reaches_the_finding():
    assert run(_app(Fatal()))[0].severity == "error"
    assert run(_app(Noisy()))[0].severity == "info"


@pytest.mark.parametrize(
    ("severity", "at_warning"), [("error", True), ("warning", True), ("info", False)]
)
def test_at_least_ranks_severities(severity, at_warning):
    assert Finding("m", severity).at_least("warning") is at_warning
    assert Finding("m", severity).at_least("info") is True


def test_an_error_finding_refuses_the_boot():
    with pytest.raises(AuditFailed) as exc:
        TestClient(_app(Fatal()))
    assert exc.value.findings[0].id == "fatal"
    assert "broken." in str(exc.value)


def test_the_boot_refusal_is_still_a_value_error():
    """A misconfigured middleware raised `ValueError` before findings existed."""
    with pytest.raises(ValueError):
        TestClient(_app(Fatal()))


def test_a_warning_does_not_refuse_the_boot():
    """Otherwise a non-Secure cookie would make local HTTP development impossible."""
    app = Veloce(openapi_url=None)
    client = TestClient(app)
    assert [f.severity for f in run(app)] == ["warning", "warning"]
    assert client.get("/nope").status_code == 404


def test_an_info_finding_does_not_fail_the_check():
    """`veloce check` exits on `warning` and above, so `info` is reportable."""
    findings = run(_app(Noisy()))
    assert findings and not any(f.at_least("warning") for f in findings)


# ── silencing ────────────────────────────────────────────────────────


def test_an_accepted_finding_is_silenced_by_id():
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    assert {f.id for f in run(app)} == {"hardening-headers-missing"}
    app.config["SILENCED_AUDIT_IDS"] = ("hardening-headers-missing",)
    assert run(app) == []


def test_silencing_one_finding_leaves_the_others():
    app = Veloce(openapi_url=None, debug=True)
    app.config["SILENCED_AUDIT_IDS"] = ("debug-enabled",)
    ids = {f.id for f in run(app)}
    assert "debug-enabled" not in ids
    assert "secret-key-missing" in ids


# ── the phase a check runs in ────────────────────────────────────────


def test_a_route_reading_check_is_skipped_before_startup():
    """`veloce check` imports the app; a startup-registered route is not there."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.use_secure_defaults()

    @app.on_startup
    async def late():
        @app.get("/late")
        async def late_route():
            return {}

    app.add_middleware(
        RateLimitMiddleware(strategy=FixedWindow(10), overrides={"/late": FixedWindow(1)})
    )

    # Imported but not started: the route does not exist yet, and the check
    # that would call it missing is not asked.
    assert run(app, routes_final=False) == []
    # Started: the route exists, so the override resolves and nothing is wrong.
    TestClient(app)
    assert run(app, routes_final=True) == []


def test_the_context_reports_the_phase():
    seen: list[bool] = []

    class Watcher(Middleware):
        def audit(self, ctx: AuditContext) -> Any:
            seen.append(ctx.routes_final)
            return ()

    app = _app(Watcher())
    run(app, routes_final=False)
    run(app, routes_final=True)
    assert seen == [False, True]


def test_a_route_reading_check_runs_once_the_table_is_final():
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.use_secure_defaults()

    @app.get("/real")
    async def real():
        return {}

    app.add_middleware(
        RateLimitMiddleware(
            strategy=FixedWindow(10),
            overrides={"/typo": FixedWindow(1)},
            strict_overrides=False,
        )
    )
    assert run(app, routes_final=False) == []
    findings = run(app, routes_final=True)
    assert [f.id for f in findings] == ["ratelimit-overrides-unknown"]
    assert findings[0].severity == "warning"


# ── rendering ────────────────────────────────────────────────────────


def test_a_finding_renders_its_message_and_remedy():
    assert str(Finding("what went wrong.", fix="what to do")) == "what went wrong. (what to do)"
    assert str(Finding("what went wrong.")) == "what went wrong."


def test_security_audit_renders_the_findings():
    app = _app(Noisy())
    assert app.security_audit() == [str(f) for f in run(app)]
