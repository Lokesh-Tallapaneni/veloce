"""Compiled feature pipeline - registry invalidation + zero-overhead invariants.

The feature registry compiles app-level features once and recompiles only when
the generation counter advances. These tests pin two contracts: every public
registration verb bumps `_gen` (so a later request observes the new feature), and
a feature-free app compiles to an all-`None`, all-flags-`False` pipeline.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.middleware.base import Middleware
from veloce.testclient import TestClient


class _TagMiddleware(Middleware):
    """Stamps a marker header so a request can observe the middleware ran."""

    async def process_response(self, request, response):
        response.headers["X-Tag-MW"] = "1"
        return response


async def _tag_http_mw(request, call_next):
    response = await call_next(request)
    response.headers["X-Tag-HTTP-MW"] = "1"
    return response


def _make_app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["TESTING"] = True

    @app.get("/")
    def index():
        return {"ok": True}

    return app


def _observe(observe: str, app: Veloce, client: TestClient, tmp_path) -> None:
    """Register one feature via its public verb and return its observation key."""
    if observe == "mw":
        app.add_middleware(_TagMiddleware())
    elif observe == "http_mw":
        app.add_http_middleware(_tag_http_mw)
    elif observe == "instrument":
        app.state.instrument_calls = []

        def hook(metrics):
            app.state.instrument_calls.append(metrics)

        app.add_instrumentation(hook)
    elif observe == "static":
        static_dir = tmp_path / "assets"
        static_dir.mkdir()
        (static_dir / "hello.txt").write_text("served", encoding="utf-8")
        app.mount_static("/s", str(static_dir))
    else:  # pragma: no cover - guards against an untested parametrize row
        raise AssertionError(f"unknown observe key {observe!r}")


def _assert_observed(observe: str, app: Veloce, client: TestClient) -> None:
    """Assert a second request observes the newly registered feature."""
    if observe == "mw":
        resp = client.get("/")
        assert resp.headers.get("X-Tag-MW") == "1"
    elif observe == "http_mw":
        resp = client.get("/")
        assert resp.headers.get("X-Tag-HTTP-MW") == "1"
    elif observe == "instrument":
        client.get("/")
        assert len(app.state.instrument_calls) >= 1
    elif observe == "static":
        # The compiled fast-path flag flips, and the asset is served.
        assert app._ensure_pipeline().has_static_handlers is True
        resp = client.get("/s/hello.txt")
        assert resp.status_code == 200
        assert resp.text == "served"


@pytest.mark.parametrize(
    "observe",
    ["mw", "http_mw", "instrument", "static"],
)
def test_late_registration_recompiles_pipeline(observe, tmp_path):
    """Each registration verb bumps `_gen`; a second request observes it.

    Table-driven over the registration verbs so a new feature adds one row, not
    a new test. `TESTING` keeps the setup lock open so late registration is
    permitted (matching the in-memory TestClient contract).
    """
    app = _make_app()
    client = TestClient(app)

    # First request compiles the pipeline before the feature exists.
    assert client.get("/").status_code == 200
    gen0 = app._gen

    _observe(observe, app, client, tmp_path)

    # The funnel bumped the generation counter, so the next access recompiles.
    assert app._gen == gen0 + 1

    _assert_observed(observe, app, client)


def test_feature_free_pipeline_is_all_none(tmp_path):
    """A feature-free app compiles to empty phase slots and false route flags."""
    app = _make_app()
    client = TestClient(app)
    assert client.get("/").status_code == 200

    cp = app._ensure_pipeline()
    assert cp.http_pre is None
    assert cp.http_post is None
    assert cp.http_around is None
    assert cp.http_finish is None
    assert cp.ws_handshake is None
    assert cp.asgi_wrap is None
    assert cp.has_mounted_apps is False
    assert cp.has_static_handlers is False
    assert cp.has_asgi_mounts is False
