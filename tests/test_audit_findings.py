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
    SecurityHeadersMiddleware,
    SessionMiddleware,
    TestClient,
    Veloce,
)
from veloce.audit import run
from veloce.ratelimit import FixedWindow


def _app(*middleware: Middleware, **kw: Any) -> Veloce:
    app = Veloce(openapi_url=None, **kw)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(
        SecurityHeadersMiddleware(
            hsts_max_age=31536000, content_security_policy="default-src 'self'"
        )
    )
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
    assert [f.severity for f in run(app)] == ["warning"]
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
    assert "hardening-headers-missing" in ids


# ── the phase a check runs in ────────────────────────────────────────


def test_a_route_reading_check_is_skipped_before_startup():
    """`veloce check` imports the app; a startup-registered route is not there."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(
        SecurityHeadersMiddleware(
            hsts_max_age=31536000, content_security_policy="default-src 'self'"
        )
    )

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
    # `routes-undocumented` is about the test routes, not this check.
    assert [f.id for f in run(app, routes_final=False) if "ratelimit" in (f.id or "")] == []
    # Started: the route exists, so the override resolves and nothing is wrong.
    TestClient(app)
    assert [f.id for f in run(app, routes_final=True) if "ratelimit" in (f.id or "")] == []


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
    app.add_middleware(
        SecurityHeadersMiddleware(
            hsts_max_age=31536000, content_security_policy="default-src 'self'"
        )
    )

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
    assert [f.id for f in run(app, routes_final=False) if "ratelimit" in (f.id or "")] == []
    findings = [f for f in run(app, routes_final=True) if "ratelimit" in (f.id or "")]
    assert [f.id for f in findings] == ["ratelimit-overrides-unknown"]
    assert findings[0].severity == "warning"


# ── rendering ────────────────────────────────────────────────────────


def test_a_finding_renders_its_message_and_remedy():
    assert str(Finding("what went wrong.", fix="what to do")) == "what went wrong. (what to do)"
    assert str(Finding("what went wrong.")) == "what went wrong."


def test_security_audit_renders_the_findings():
    app = _app(Noisy())
    assert app.security_audit() == [str(f) for f in run(app)]


# ── config that no longer configures ─────────────────────────────────


@pytest.mark.parametrize(
    "key", ["SESSION_COOKIE_SECURE", "SESSION_COOKIE_NAME", "SESSION_COOKIE_HTTPONLY"]
)
def test_a_retired_session_config_key_refuses_the_boot(key):
    """The upgrade hazard, closed.

    `SESSION_COOKIE_SECURE = True` plus a bare `SessionMiddleware()` used to
    produce a `Secure` cookie. The constructor is the only source now, so the
    same code would produce a plain one - silently, on a security setting. It
    stops the boot instead.
    """
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.config[key] = True
    app.add_middleware(SessionMiddleware(secret_key="k"))
    with pytest.raises(AuditFailed) as exc:
        TestClient(app)
    assert exc.value.findings[0].id == "session-config-retired"
    assert key in str(exc.value)


def test_a_secure_cookie_set_the_new_way_is_clean():
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(SessionMiddleware(secret_key="k", secure=True))
    app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=31536000))
    assert [f for f in run(app) if f.at_least("warning")] == []


def test_the_headers_middleware_reports_what_it_is_not_sending():
    """The judgment about HSTS lives with the header, not in an app helper."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(SecurityHeadersMiddleware())
    ids = {f.id for f in run(app)}
    assert ids == {"hsts-not-sent", "csp-not-sent"}
    assert all(f.severity == "info" for f in run(app))


def test_a_configured_header_is_not_reported():
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(
        SecurityHeadersMiddleware(
            hsts_max_age=31536000, content_security_policy="default-src 'self'"
        )
    )
    assert run(app) == []


# ── the secure-by-default preset, end to end ─────────────────
#
# Moved here from `test_app.py`, where these sat in a bare-function tail whose
# sections were labelled by internal batch id (`S7:`, `P-6:`).


def test_security_audit_flags_insecure_app():
    insecure = Veloce(debug=True, openapi_url=None)
    insecure.add_middleware(SessionMiddleware(secret_key="k" * 32))
    warnings = insecure.security_audit()
    assert any("DEBUG" in w for w in warnings)
    # The session cookie is the app-level posture the audit reports on; the
    # signing key belongs to the middleware, which reports it itself.
    assert any("not Secure" in w for w in warnings)


def test_security_audit_clean_after_hardening():
    secured = Veloce(openapi_url=None)
    secured.config["SECRET_KEY"] = "a-real-secret"
    secured.add_middleware(
        SecurityHeadersMiddleware(
            hsts_max_age=31536000, content_security_policy="default-src 'self'"
        )
    )
    assert secured.security_audit() == []
