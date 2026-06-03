"""Tests for the optional Prometheus exporter (``veloce.metrics``).

``prometheus_client`` is an optional dependency and is not installed in the
default test environment. These tests cover the behaviour that is verifiable
without it: that importing the package and the exporter module both succeed
with prometheus_client absent, the shape of the import-error sentinel and
install hint, and that calling the factory without prometheus_client raises a
clear ImportError naming the extra. The end-to-end series-recording path
requires prometheus_client and is guarded with importorskip; it drives real
requests through ``add_instrumentation`` / ``_run_instrumentation`` and asserts
the expected samples on a fresh registry.
"""

from __future__ import annotations

import importlib

import pytest

from veloce import Veloce


def test_import_veloce_succeeds_without_prometheus() -> None:
    # Importing the package must never require prometheus_client.
    module = importlib.import_module("veloce")
    assert hasattr(module, "Veloce")


def test_import_metrics_module_succeeds_without_prometheus() -> None:
    # Importing the exporter module itself must not hard-crash when
    # prometheus_client is absent — the import is guarded.
    metrics = importlib.import_module("veloce.metrics")
    assert hasattr(metrics, "instrument_with_prometheus")
    assert hasattr(metrics, "_PROM_IMPORT_ERROR")
    assert hasattr(metrics, "_INSTALL_HINT")


def test_import_error_sentinel_shape() -> None:
    import veloce.metrics as metrics

    # The sentinel is either None (installed) or an ImportError (absent).
    assert metrics._PROM_IMPORT_ERROR is None or isinstance(metrics._PROM_IMPORT_ERROR, ImportError)


def test_install_hint_names_the_optional_extra() -> None:
    import veloce.metrics as metrics

    assert "veloceframework[metrics]" in metrics._INSTALL_HINT


def test_factory_without_prometheus_raises_importerror() -> None:
    # Without prometheus_client the factory must refuse with a clear, actionable
    # error rather than failing obscurely deeper in the exporter.
    import veloce.metrics as metrics

    if metrics._PROM_IMPORT_ERROR is None:
        pytest.skip("prometheus_client is installed; the no-prometheus path is not exercised")

    app = Veloce(openapi_url=None)
    with pytest.raises(ImportError) as excinfo:
        metrics.instrument_with_prometheus(app)

    assert "veloceframework[metrics]" in str(excinfo.value)


def test_factory_refuses_when_prometheus_marked_absent(monkeypatch) -> None:
    # Deterministic no-prometheus path: force the import sentinel to an
    # ImportError so this runs even where prometheus_client IS installed. The
    # factory must refuse with the install hint, never proceed.
    import veloce.metrics as metrics

    monkeypatch.setattr(
        metrics, "_PROM_IMPORT_ERROR", ImportError("no module named prometheus_client")
    )
    app = Veloce(openapi_url=None)
    with pytest.raises(ImportError) as excinfo:
        metrics.instrument_with_prometheus(app)
    assert "veloceframework[metrics]" in str(excinfo.value)


def _app() -> Veloce:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/items/{item_id}")
    async def get_item(item_id: int):
        return {"id": item_id}

    @app.get("/crash")
    async def crash():
        raise ValueError("kaboom")

    return app


def test_records_total_and_duration_with_template_route_labels() -> None:
    pytest.importorskip("prometheus_client")

    from prometheus_client import CollectorRegistry

    from veloce.metrics import instrument_with_prometheus

    app = _app()
    registry = CollectorRegistry()
    # Keep concrete status codes so the per-code assertions below hold.
    instrument_with_prometheus(app, registry=registry, group_status=False)

    client = app.test_client()
    assert client.get("/items/5").status_code == 200
    missing_path = "/missing-unique-marker-9f3a"
    assert client.get(missing_path).status_code == 404
    assert client.get("/crash").status_code == 500

    # Route is the TEMPLATE, not the concrete /items/5 — the core safe
    # cardinality assertion.
    assert (
        registry.get_sample_value(
            "http_requests_total",
            {"method": "GET", "route": "/items/{item_id}", "status": "200"},
        )
        == 1.0
    )
    # A sample was observed in the duration histogram for the same template.
    assert (
        registry.get_sample_value(
            "http_request_duration_seconds_count",
            {"method": "GET", "route": "/items/{item_id}"},
        )
        == 1.0
    )
    # The 404 carries the constant "<unmatched>" route label.
    assert (
        registry.get_sample_value(
            "http_requests_total",
            {"method": "GET", "route": "<unmatched>", "status": "404"},
        )
        == 1.0
    )
    # The concrete unmatched path must never appear as a label value anywhere.
    for metric in registry.collect():
        for sample in metric.samples:
            assert sample.labels.get("route") != missing_path
            assert missing_path not in sample.labels.values()
    # The handler crash is recorded as a 500 against its own route template.
    assert (
        registry.get_sample_value(
            "http_requests_total",
            {"method": "GET", "route": "/crash", "status": "500"},
        )
        == 1.0
    )


def test_group_status_collapses_codes_into_class_buckets() -> None:
    # group_status=True (the default) must fold the concrete code into its
    # class bucket, so two different 2xx codes share a single status="2xx"
    # series instead of one series per code.
    pytest.importorskip("prometheus_client")

    from prometheus_client import CollectorRegistry

    from veloce import Response
    from veloce.metrics import instrument_with_prometheus

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/ok")
    async def ok():
        return Response(body=b"ok", status_code=200)

    @app.get("/created")
    async def created():
        return Response(body=b"made", status_code=201)

    registry = CollectorRegistry()
    instrument_with_prometheus(app, registry=registry, group_status=True)

    client = app.test_client()
    assert client.get("/ok").status_code == 200
    assert client.get("/created").status_code == 201

    # Both 2xx responses collapse to one status="2xx" series; one hit per route.
    assert (
        registry.get_sample_value(
            "http_requests_total",
            {"method": "GET", "route": "/ok", "status": "2xx"},
        )
        == 1.0
    )
    assert (
        registry.get_sample_value(
            "http_requests_total",
            {"method": "GET", "route": "/created", "status": "2xx"},
        )
        == 1.0
    )
    # The concrete codes must NOT appear as label values when grouping is on.
    for metric in registry.collect():
        for sample in metric.samples:
            assert sample.labels.get("status") not in {"200", "201"}


def test_returns_registered_hook() -> None:
    pytest.importorskip("prometheus_client")

    from prometheus_client import CollectorRegistry

    from veloce.metrics import instrument_with_prometheus

    app = Veloce(openapi_url=None)
    hook = instrument_with_prometheus(app, registry=CollectorRegistry())
    # The returned hook is the exact object appended to the app's instrumentation
    # list — mirroring the otel bridge's return-hook contract.
    assert app._instrumentation[-1] is hook


def test_custom_prefix() -> None:
    pytest.importorskip("prometheus_client")

    from prometheus_client import CollectorRegistry

    from veloce.metrics import instrument_with_prometheus

    app = _app()
    registry = CollectorRegistry()
    instrument_with_prometheus(app, prefix="api", registry=registry, group_status=False)

    app.test_client().get("/items/5")
    assert (
        registry.get_sample_value(
            "api_requests_total",
            {"method": "GET", "route": "/items/{item_id}", "status": "200"},
        )
        == 1.0
    )


def test_separate_registries_no_duplicate_error() -> None:
    # Two apps instrumented against two distinct registries must not raise
    # prometheus_client's "Duplicated timeseries" error — validating the
    # instance-local, registry-bound design over a global shared collector.
    pytest.importorskip("prometheus_client")

    from prometheus_client import CollectorRegistry

    from veloce.metrics import instrument_with_prometheus

    app1 = _app()
    app2 = _app()
    instrument_with_prometheus(app1, registry=CollectorRegistry())
    instrument_with_prometheus(app2, registry=CollectorRegistry())

    assert app1.test_client().get("/items/1").status_code == 200
    assert app2.test_client().get("/items/2").status_code == 200
