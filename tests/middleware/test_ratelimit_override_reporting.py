"""Whether an unmatched override key is fatal is decided once, at startup.

`SILENCED_AUDIT_IDS` is the documented way to accept a finding without turning
the audit off. For this one it did the opposite of accepting it: the audit's
`error` was silenced, so the boot succeeded — and then the same check, still
raising on the request path, turned every single request into a 500.

Using the escape hatch was strictly worse than not using it. A key that matches
no route means an override is inactive; refusing to answer any request at all is
never the proportionate response to that.
"""

from __future__ import annotations

import logging

import pytest

from veloce import RateLimitMiddleware, Veloce
from veloce.audit import AuditFailed, run
from veloce.ratelimit import FixedWindow
from veloce.testclient import TestClient


def _app(*, strict: bool = True, silence: bool = False, key: str = "/nope") -> Veloce:
    app = Veloce(openapi_url=None)
    if silence:
        app.config["SILENCED_AUDIT_IDS"] = ("ratelimit-overrides-unknown",)

    @app.get("/real")
    async def real():
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware(
            strategy=FixedWindow(100),
            overrides={key: FixedWindow(1)},
            strict_overrides=strict,
        )
    )
    return app


# ── the four combinations ────────────────────────────────────────────


def test_strict_and_not_silenced_refuses_the_boot():
    with pytest.raises(AuditFailed, match="match no registered route"):
        TestClient(_app(strict=True))


def test_strict_but_silenced_serves_requests():
    """The defect: this booted and then 500ed every request."""
    client = TestClient(_app(strict=True, silence=True))
    assert client.get("/real").status_code == 200


def test_not_strict_serves_requests():
    client = TestClient(_app(strict=False))
    assert client.get("/real").status_code == 200


def test_not_strict_and_silenced_serves_requests():
    client = TestClient(_app(strict=False, silence=True))
    assert client.get("/real").status_code == 200


def test_silencing_never_makes_things_worse_than_not_silencing():
    """The property the defect violated, stated directly."""
    loud = _app(strict=True, silence=False)
    quiet = _app(strict=True, silence=True)
    with pytest.raises(AuditFailed):
        TestClient(loud)
    assert TestClient(quiet).get("/real").status_code == 200


# ── the severity is still decided, and still correct ─────────────────


def test_strict_reports_an_error():
    assert [
        f.severity
        for f in run(_app(strict=True), routes_final=True)
        if f.id == "ratelimit-overrides-unknown"
    ] == ["error"]


def test_non_strict_reports_a_warning():
    assert [
        f.severity
        for f in run(_app(strict=False), routes_final=True)
        if f.id == "ratelimit-overrides-unknown"
    ] == ["warning"]


def test_a_matching_override_reports_nothing():
    app = Veloce(openapi_url=None)

    @app.get("/real")
    async def real():
        return {}

    app.add_middleware(
        RateLimitMiddleware(strategy=FixedWindow(100), overrides={"/real": FixedWindow(1)})
    )
    assert [f.id for f in run(app, routes_final=True) if "ratelimit" in (f.id or "")] == []


def test_the_finding_names_the_unmatched_key():
    finding = next(
        f
        for f in run(_app(key="/typo"), routes_final=True)
        if f.id == "ratelimit-overrides-unknown"
    )
    assert "/typo" in str(finding)


# ── the override that does match still applies ───────────────────────


def test_a_matching_override_is_enforced():
    """Reporting must not have cost the feature its behaviour."""
    app = Veloce(openapi_url=None)

    @app.get("/tight")
    async def tight():
        return {}

    @app.get("/loose")
    async def loose():
        return {}

    app.add_middleware(
        RateLimitMiddleware(strategy=FixedWindow(100), overrides={"/tight": FixedWindow(1)})
    )
    client = TestClient(app)
    assert client.get("/tight").status_code == 200
    assert client.get("/tight").status_code == 429
    assert client.get("/loose").status_code == 200


def test_an_inactive_override_leaves_the_default_strategy_in_force():
    client = TestClient(_app(strict=False))
    assert client.get("/real").status_code == 200


# ── the log, and how often it appears ────────────────────────────────


def test_an_unmatched_key_is_logged_when_requests_arrive(caplog):
    client = TestClient(_app(strict=False))
    with caplog.at_level(logging.WARNING):
        client.get("/real")
    assert any("match no registered route" in r.getMessage() for r in caplog.records)


def test_the_same_unmatched_key_is_not_logged_on_every_request(caplog):
    """It runs on every route-table rebuild; a per-request log would flood."""
    client = TestClient(_app(strict=False))
    with caplog.at_level(logging.WARNING):
        for _ in range(20):
            client.get("/real")
    matching = [r for r in caplog.records if "match no registered route" in r.getMessage()]
    assert len(matching) == 1


def test_a_matching_override_logs_nothing(caplog):
    app = Veloce(openapi_url=None)

    @app.get("/real")
    async def real():
        return {}

    app.add_middleware(
        RateLimitMiddleware(strategy=FixedWindow(100), overrides={"/real": FixedWindow(1)})
    )
    client = TestClient(app)
    with caplog.at_level(logging.WARNING):
        client.get("/real")
    assert [r for r in caplog.records if "match no registered route" in r.getMessage()] == []


# ── edges ────────────────────────────────────────────────────────────


def test_no_overrides_at_all_reports_nothing():
    app = Veloce(openapi_url=None)

    @app.get("/real")
    async def real():
        return {}

    app.add_middleware(RateLimitMiddleware(strategy=FixedWindow(100)))
    assert [f.id for f in run(app, routes_final=True) if "ratelimit" in (f.id or "")] == []


def test_the_check_is_skipped_before_the_route_table_is_final():
    """`veloce check` imports the app; a startup-registered route is not there."""
    app = Veloce(openapi_url=None)

    @app.on_startup
    async def late():
        @app.get("/late")
        async def late_route():
            return {}

    app.add_middleware(
        RateLimitMiddleware(strategy=FixedWindow(100), overrides={"/late": FixedWindow(1)})
    )
    assert [f.id for f in run(app, routes_final=False) if "ratelimit" in (f.id or "")] == []
    TestClient(app)
    assert [f.id for f in run(app, routes_final=True) if "ratelimit" in (f.id or "")] == []


def test_several_unmatched_keys_are_all_named():
    app = Veloce(openapi_url=None)

    @app.get("/real")
    async def real():
        return {}

    app.add_middleware(
        RateLimitMiddleware(
            strategy=FixedWindow(100),
            overrides={"/a": FixedWindow(1), "/b": FixedWindow(1)},
            strict_overrides=False,
        )
    )
    finding = next(f for f in run(app, routes_final=True) if f.id == "ratelimit-overrides-unknown")
    assert "/a" in str(finding) and "/b" in str(finding)
