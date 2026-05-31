"""Tests for the optional OpenTelemetry bridge (``veloce.otel``).

OpenTelemetry is an optional dependency and is not installed in the default
test environment. These tests cover the behaviour that is verifiable without
it: that importing the package and the bridge module both succeed with
OpenTelemetry absent, the shape of the import-error sentinel and install hint,
and that calling the factory without OpenTelemetry raises a clear ImportError
naming the extra. The end-to-end span-emission path requires OpenTelemetry and
is guarded with importorskip; it drives a real request through
``add_instrumentation`` / ``_run_instrumentation`` and asserts one span with the
expected name and attributes using an in-memory exporter.
"""

from __future__ import annotations

import importlib

import pytest

from veloce import Veloce


def test_import_veloce_succeeds_without_opentelemetry() -> None:
    # Importing the package must never require OpenTelemetry.
    module = importlib.import_module("veloce")
    assert hasattr(module, "Veloce")


def test_import_otel_module_succeeds_without_opentelemetry() -> None:
    # Importing the bridge module itself must not hard-crash when OpenTelemetry
    # is absent — the API import is guarded.
    otel = importlib.import_module("veloce.otel")
    assert hasattr(otel, "instrument_with_otel")
    assert hasattr(otel, "_OTEL_IMPORT_ERROR")
    assert hasattr(otel, "_INSTALL_HINT")


def test_import_error_sentinel_shape() -> None:
    import veloce.otel as otel

    # The sentinel is either None (installed) or an ImportError (absent).
    assert otel._OTEL_IMPORT_ERROR is None or isinstance(otel._OTEL_IMPORT_ERROR, ImportError)


def test_install_hint_names_the_optional_extra() -> None:
    import veloce.otel as otel

    assert "veloceframework[otel]" in otel._INSTALL_HINT


def test_factory_without_opentelemetry_raises_importerror() -> None:
    # Without OpenTelemetry the factory must refuse with a clear, actionable
    # error rather than failing obscurely deeper in the bridge.
    import veloce.otel as otel

    if otel._OTEL_IMPORT_ERROR is None:
        pytest.skip("opentelemetry is installed; the no-otel path is not exercised")

    app = Veloce(openapi_url=None)
    with pytest.raises(ImportError) as excinfo:
        otel.instrument_with_otel(app)

    assert "veloceframework[otel]" in str(excinfo.value)


def _app() -> Veloce:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/items/{item_id}")
    async def get_item(item_id: int):
        return {"id": item_id}

    @app.get("/crash")
    async def crash():
        raise ValueError("kaboom")

    return app


def _exporter_and_app():
    """Build an in-memory exporter wired to an instrumented `_app()`."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from veloce.otel import instrument_with_otel

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = _app()
    instrument_with_otel(app, tracer_provider=provider)
    return exporter, app


def test_emits_server_span_per_request() -> None:
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.trace import SpanKind

    from veloce.otel import instrument_with_otel

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = _app()
    instrument_with_otel(app, tracer_provider=provider)

    app.test_client().get("/items/7")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "/items/{item_id}"
    assert span.kind == SpanKind.SERVER
    assert span.attributes["http.request.method"] == "GET"
    assert span.attributes["http.route"] == "/items/{item_id}"
    assert span.attributes["http.response.status_code"] == 200
    assert "duration_ms" in span.attributes


def test_span_is_backdated_to_the_measured_request_window() -> None:
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    import time

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from veloce.otel import instrument_with_otel

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = _app()
    instrument_with_otel(app, tracer_provider=provider)

    # Bracket the request with wall-clock readings so we can prove the exported
    # span timestamps reflect the real request window, not just a duration_ms
    # attribute. start_time/end_time are integer nanoseconds since the epoch.
    before = time.time_ns()
    app.test_client().get("/items/7")
    after = time.time_ns()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    # The span has real, distinct, ordered timestamps.
    assert span.start_time is not None
    assert span.end_time is not None
    assert span.end_time >= span.start_time

    # The whole window falls inside the bracket we measured around the request.
    assert before <= span.start_time
    assert span.end_time <= after

    # The exported window matches the duration_ms attribute (within 1ms of
    # rounding), proving the backdate is derived from the measured duration.
    exported_ms = (span.end_time - span.start_time) / 1_000_000
    assert abs(exported_ms - span.attributes["duration_ms"]) <= 1.0


def test_span_is_root_and_ignores_ambient_context() -> None:
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from opentelemetry import context as otel_context
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from veloce.otel import instrument_with_otel

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = _app()
    instrument_with_otel(app, tracer_provider=provider)

    # Make an unrelated span the ambient current context while the request runs.
    # The retroactive request span must NOT pick this up as its parent.
    tracer = provider.get_tracer("test-ambient")
    ambient = tracer.start_span("ambient", kind=trace.SpanKind.INTERNAL)
    token = otel_context.attach(trace.set_span_in_context(ambient))
    try:
        app.test_client().get("/items/7")
    finally:
        otel_context.detach(token)
        ambient.end()

    request_spans = [s for s in exporter.get_finished_spans() if s.name == "/items/{item_id}"]
    assert len(request_spans) == 1
    request_span = request_spans[0]

    # A clean server root: no parent, and specifically not the ambient span.
    assert request_span.parent is None
    assert request_span.context.trace_id != ambient.get_span_context().trace_id


def test_5xx_marks_span_error() -> None:
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.trace import StatusCode

    from veloce.otel import instrument_with_otel

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = _app()
    instrument_with_otel(app, tracer_provider=provider)

    app.test_client().get("/crash")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["http.response.status_code"] == 500
    assert span.status.status_code == StatusCode.ERROR


# ── streamed responses are skipped ────────────────────────────────────


def test_no_span_for_delayed_streaming_response() -> None:
    """A streaming body is emitted on the ASGI send path *after* the
    instrumentation hook fires, so the available timing/status would be wrong.
    The bridge must skip such records and export no span — even when the
    stream is artificially delayed between chunks."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    import asyncio

    from veloce.http.response import StreamingResponse

    exporter, app = _exporter_and_app()

    @app.get("/slow-stream")
    async def slow_stream():
        async def gen():
            for i in range(3):
                await asyncio.sleep(0.01)
                yield f"chunk-{i}".encode()

        return StreamingResponse(gen())

    resp = app.test_client().get("/slow-stream")
    assert resp.status_code == 200
    assert resp.body == b"chunk-0chunk-1chunk-2"

    # The hook fired (the request finished) but the bridge emitted no span.
    assert exporter.get_finished_spans() == ()


def test_no_span_when_stream_fails_mid_body() -> None:
    """A failure raised after some chunks have been sent cannot be reflected
    in the post-dispatch metrics record (status was already 200, timing was
    already taken). The bridge must not export a misleading 'successful' span;
    skipping streamed records guarantees no span is emitted at all."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from veloce.http.response import StreamingResponse

    exporter, app = _exporter_and_app()

    @app.get("/broken-stream")
    async def broken_stream():
        async def gen():
            yield b"partial"
            raise RuntimeError("stream blew up mid-body")

        return StreamingResponse(gen())

    # The mid-stream failure propagates out of the ASGI emit path; the request
    # has already been instrumented (streamed=True) by the time it fires.
    with pytest.raises(RuntimeError, match="stream blew up mid-body"):
        app.test_client().get("/broken-stream")

    # No span — a streamed record never produces one, so a half-sent, failed
    # stream is never exported as a clean 200.
    assert exporter.get_finished_spans() == ()


# ── unmatched requests use a low-cardinality fallback name ─────────────


def test_404_emits_no_raw_path_in_span_name_or_attributes() -> None:
    """An unknown path (404) has no matched route. The span name must be a
    stable, method-based fallback — never the attacker-controlled concrete
    path — and the raw path must not leak into any attribute."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    exporter, app = _exporter_and_app()

    secret_path = "/no/such/path/with-a-very-unique-marker-9f3a"
    resp = app.test_client().get(secret_path)
    assert resp.status_code == 404

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    # Low-cardinality method-based fallback name, not the path.
    assert span.name == "HTTP GET"
    assert secret_path not in span.name

    # No http.route for an unmatched request, and the path appears nowhere.
    assert "http.route" not in span.attributes
    for value in span.attributes.values():
        assert value != secret_path
    assert span.attributes["http.request.method"] == "GET"
    assert span.attributes["http.response.status_code"] == 404


def test_405_emits_method_fallback_name_without_raw_path() -> None:
    """A 405 (path exists, wrong method) also has no matched route. It must
    use the same method-based fallback and keep the raw path out of the
    export."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    exporter, app = _exporter_and_app()

    # /items/{item_id} exists for GET; DELETE is not allowed -> 405.
    resp = app.test_client().delete("/items/42")
    assert resp.status_code == 405

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.name == "HTTP DELETE"
    assert "http.route" not in span.attributes
    for value in span.attributes.values():
        assert value != "/items/42"
    assert span.attributes["http.response.status_code"] == 405
