"""Liveness and readiness probes.

The two probes answer different questions and must fail independently: a
failing readiness check pulls the replica out of the load-balancer pool, while
a failing liveness check gets the container killed. Tying them together is the
usual cause of a dependency blip restarting every replica at once.
"""

from __future__ import annotations

import asyncio

from veloce import Veloce
from veloce.health import HealthPlugin
from veloce.testclient import TestClient


def test_liveness_is_200_with_no_checks_registered():
    app = Veloce(openapi_url=None)
    app.install(HealthPlugin())
    resp = TestClient(app).get("/livez")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_readiness_is_200_when_every_check_passes():
    app = Veloce(openapi_url=None)
    health = app.install(HealthPlugin())

    @health.readiness_check("database")
    async def db() -> bool:
        return True

    @health.readiness_check("cache")
    def cache() -> bool:  # sync checks are accepted too
        return True

    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "checks": {"database": "pass", "cache": "pass"}}


def test_readiness_names_the_failing_check():
    app = Veloce(openapi_url=None)
    health = app.install(HealthPlugin())

    @health.readiness_check("database")
    async def db() -> bool:
        return False

    @health.readiness_check("cache")
    async def cache() -> bool:
        return True

    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"] == {"database": "fail", "cache": "pass"}


def test_a_raising_check_counts_as_not_ready():
    """A probe that 500s tells the orchestrator nothing actionable."""
    app = Veloce(openapi_url=None)
    health = app.install(HealthPlugin())

    @health.readiness_check("database")
    async def db() -> bool:
        raise RuntimeError("connection refused")

    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["database"] == "fail"


def test_a_hanging_check_fails_the_probe_rather_than_holding_it_open():
    app = Veloce(openapi_url=None)
    health = app.install(HealthPlugin(timeout=0.05))

    @health.readiness_check("slow")
    async def slow() -> bool:
        await asyncio.sleep(5)
        return True

    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["slow"] == "fail"


def test_draining_fails_readiness_but_keeps_liveness_passing():
    """The whole point of two probes: stop new traffic without being killed
    mid-drain."""
    app = Veloce(openapi_url=None)
    health = app.install(HealthPlugin())
    client = TestClient(app)

    assert client.get("/readyz").status_code == 200
    health.start_draining()

    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "reason": "draining"}
    assert client.get("/livez").status_code == 200


def test_probe_paths_are_configurable():
    app = Veloce(openapi_url=None)
    app.install(HealthPlugin(liveness_path="/healthz", readiness_path="/ready"))
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/ready").status_code == 200


def test_probes_stay_out_of_the_openapi_schema_by_default():
    app = Veloce()
    app.install(HealthPlugin())
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    assert "/livez" not in paths
    assert "/readyz" not in paths


def test_the_plugin_is_reachable_through_extensions():
    app = Veloce(openapi_url=None)
    health = app.install(HealthPlugin())
    assert app.extensions["health"] is health
