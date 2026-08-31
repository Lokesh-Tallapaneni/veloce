"""Only the session middleware knows whether it has a signing key.

The audit read `SECRET_KEY` from config, which is the wrong place to ask, and it
got both directions wrong from there.

It warned about a middleware constructed with an explicit `secret_key=` — one
that was already signing correctly. A security tool that cries wolf is ignored
wholesale, so a false warning costs more than the check is worth.

And it was only a *warning* for a middleware that had no key from either source
— which cannot sign a single cookie. Startup succeeded, health checks passed,
the container went into rotation, and then every request through it raised.
That is now an `error`, which refuses the boot.
"""

from __future__ import annotations

import pytest

from veloce import (
    SecurityHeadersMiddleware,
    ServerSessionMiddleware,
    SessionMiddleware,
    Veloce,
)
from veloce.audit import AuditFailed, run
from veloce.testclient import TestClient

KEY = "k" * 32


def _app(*middleware, **config) -> Veloce:
    app = Veloce(openapi_url=None)
    app.config.update(config)
    app.add_middleware(
        SecurityHeadersMiddleware(
            hsts_max_age=31536000, content_security_policy="default-src 'self'"
        )
    )
    for mw in middleware:
        app.add_middleware(mw)
    return app


def _secret_findings(app: Veloce):
    return [(f.id, f.severity) for f in run(app) if "secret" in (f.id or "")]


# ── the false warning ────────────────────────────────────────────────


def test_an_explicit_secret_key_is_not_warned_about():
    """The defect: this middleware signs correctly and was warned about."""
    assert _secret_findings(_app(SessionMiddleware(secret_key=KEY, secure=True))) == []


def test_an_explicit_secret_key_leaves_the_whole_audit_clean():
    assert run(_app(SessionMiddleware(secret_key=KEY, secure=True))) == []


def test_a_key_from_app_config_is_not_warned_about():
    app = _app(SessionMiddleware(secure=True), SECRET_KEY=KEY)
    assert _secret_findings(app) == []


def test_a_key_set_through_the_property_is_not_warned_about():
    """`app.secret_key = ...` is the documented spelling."""
    app = Veloce(openapi_url=None)
    app.secret_key = KEY
    app.add_middleware(SessionMiddleware(secure=True))
    assert _secret_findings(app) == []


def test_an_app_with_no_session_middleware_is_not_warned_about():
    """Nothing else reads the key, so nothing has standing to ask for it."""
    assert _secret_findings(_app()) == []


def test_the_server_side_backend_needs_no_signing_key():
    """Its cookie carries an opaque id, not a signed payload."""
    assert _secret_findings(_app(ServerSessionMiddleware(secure=True))) == []


# ── the fail-late case, now fail-fast ────────────────────────────────


def test_a_backend_with_no_key_at_all_is_an_error():
    findings = _secret_findings(_app(SessionMiddleware(secure=True)))
    assert findings == [("session-secret-key-missing", "error")]


def test_a_backend_with_no_key_refuses_the_boot():
    """The defect: this booted clean and then 500ed every request."""
    app = _app(SessionMiddleware(secure=True))

    @app.get("/x")
    async def x(request):
        return {"n": request.session.get("n")}

    with pytest.raises(AuditFailed) as exc:
        TestClient(app)
    assert exc.value.findings[0].id == "session-secret-key-missing"


def test_the_message_names_both_ways_to_supply_a_key():
    app = _app(SessionMiddleware(secure=True))
    finding = next(f for f in run(app) if f.id == "session-secret-key-missing")
    assert "secret_key=" in str(finding)
    assert "app.secret_key" in str(finding)


def test_an_empty_string_key_counts_as_missing():
    """A falsey key cannot sign; it must not read as configured."""
    app = _app(SessionMiddleware(secure=True), SECRET_KEY="")
    assert _secret_findings(app) == [("session-secret-key-missing", "error")]


# ── the shared checks still run alongside ────────────────────────────


def test_the_cookie_security_check_still_runs_for_the_same_middleware():
    """`audit` on the subclass extends the base rather than replacing it."""
    app = _app(SessionMiddleware(secret_key=KEY))
    assert any(f.id == "session-cookie-insecure" for f in run(app))


def test_a_retired_key_is_still_reported_for_the_same_middleware():
    app = _app(SessionMiddleware(secret_key=KEY, secure=True), MAX_COOKIE_SIZE=2048)
    assert any(f.id == "session-config-retired" for f in run(app))


def test_both_the_missing_key_and_the_insecure_cookie_are_reported():
    findings = {f.id for f in run(_app(SessionMiddleware()))}
    assert "session-secret-key-missing" in findings
    assert "session-cookie-insecure" in findings


# ── end to end ───────────────────────────────────────────────────────


def test_a_signed_session_round_trips_when_a_key_is_present():
    app = _app(SessionMiddleware(secret_key=KEY))

    @app.get("/login")
    async def login(request):
        request.session["user"] = "u"
        return {}

    @app.get("/who")
    async def who(request):
        return {"user": request.session.get("user")}

    client = TestClient(app)
    client.get("/login")
    assert client.get("/who").json() == {"user": "u"}


def test_the_missing_key_finding_can_be_silenced():
    """Silencing restores the old fail-late behaviour, deliberately chosen."""
    app = _app(SessionMiddleware(secure=True))
    app.config["SILENCED_AUDIT_IDS"] = ("session-secret-key-missing",)
    assert _secret_findings(app) == []


def test_the_app_level_secret_key_finding_is_gone():
    """Replaced by the middleware's own, which knows both sources."""
    ids = {f.id for f in run(_app(SessionMiddleware(secret_key=KEY, secure=True)))}
    assert "secret-key-missing" not in ids
