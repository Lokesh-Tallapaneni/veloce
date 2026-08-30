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


def test_build_trace_carrier_shared_tail() -> None:
    # The shared carrier tail underpins both the framework-core header reader
    # and the otel bridge's raw-scope reader; verify the three cases directly
    # (no OpenTelemetry needed). Absent traceparent -> None so callers skip
    # extraction; present -> a {traceparent[, tracestate]} dict.
    from veloce._protocol_constants import build_trace_carrier

    assert build_trace_carrier(None, None) is None
    assert build_trace_carrier(None, "vendor=1") is None
    assert build_trace_carrier("00-trace-span-01", None) == {"traceparent": "00-trace-span-01"}
    assert build_trace_carrier("00-trace-span-01", "vendor=1") == {
        "traceparent": "00-trace-span-01",
        "tracestate": "vendor=1",
    }


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


def _exporter() -> tuple:
    """An in-memory exporter and a provider feeding it.

    The half shared by tests that build their own application - one with
    `debug=True`, one that registers a hook before the bridge - and so cannot
    use `_traced_app`.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


def _traced_app(*, live: bool = False, **kwargs):
    """An in-memory exporter, its provider, and an instrumented `_app()`.

    This four-import, six-statement construction was re-inlined at a dozen test
    sites. It is the setup, not the subject: what each of those tests is about
    is the spans that come out.
    """

    from veloce.otel import instrument_with_otel

    exporter, provider = _exporter()

    app = _app()
    if live:
        instrument_with_otel(app, tracer_provider=provider, live=True, **kwargs)
    else:
        instrument_with_otel(app, tracer_provider=provider, **kwargs)
    return exporter, provider, app


def _exporter_and_app():
    """Build an in-memory exporter wired to an instrumented `_app()`."""
    exporter, _provider, app = _traced_app()
    return exporter, app


def test_emits_server_span_per_request() -> None:
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from opentelemetry.trace import SpanKind

    exporter, provider, app = _traced_app()

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

    exporter, provider, app = _traced_app()

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

    exporter, provider, app = _traced_app()

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

    from opentelemetry.trace import StatusCode

    exporter, provider, app = _traced_app()

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


def test_factory_refuses_when_otel_marked_absent(monkeypatch) -> None:
    # Deterministic no-OpenTelemetry path: force the import sentinel to an
    # ImportError so this runs even in an environment where opentelemetry IS
    # installed. The factory must refuse with the install hint, never proceed.
    import veloce.otel as otel

    monkeypatch.setattr(otel, "_OTEL_IMPORT_ERROR", ImportError("no module named opentelemetry"))
    app = Veloce(openapi_url=None)
    with pytest.raises(ImportError) as excinfo:
        otel.instrument_with_otel(app)
    assert "veloceframework[otel]" in str(excinfo.value)


def test_arbitrary_method_does_not_explode_span_name() -> None:
    # A non-standard (attacker-controlled) HTTP verb on an unmatched route must
    # NOT appear in the span name — it collapses to a single constant so it
    # cannot blow up span-name cardinality.
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    exporter, app = _exporter_and_app()
    # Drive an arbitrary method at an unmatched path (route is None → fallback).
    app.test_client()._make_request("CUSTOMVERB-9F3A", "/nope")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    # The span NAME is the cardinality-sensitive field (backends index it);
    # an arbitrary verb must never reach it — it collapses to a constant.
    assert spans[0].name == "HTTP other"
    assert "CUSTOMVERB-9F3A" not in spans[0].name
    # The real method is still recorded as an attribute (OTel semconv) — the
    # correct place for it; attribute values are not a span-name index.
    assert spans[0].attributes["http.request.method"] == "CUSTOMVERB-9F3A"


def test_standard_method_keeps_its_name_in_fallback() -> None:
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    exporter, app = _exporter_and_app()
    app.test_client().get("/nope")  # 404 → route None → fallback to method
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "HTTP GET"


def test_span_end_time_is_not_shifted_by_a_slow_earlier_hook() -> None:
    # Hold: the span window must anchor to the end captured at dispatch, not
    # the moment the OTel hook runs — so a slow EARLIER instrumentation hook
    # cannot push the span's end_time past the real request boundary.
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")
    import time

    from veloce.otel import instrument_with_otel

    exporter, provider = _exporter()
    app = _app()

    # A slow instrumentation hook registered BEFORE the OTel bridge: it runs
    # first, so a now()-anchored span end would land after it.
    entered: list[int] = []

    def _slow_hook(metrics):
        entered.append(time.time_ns())
        time.sleep(0.01)

    app.add_instrumentation(_slow_hook)
    instrument_with_otel(app, tracer_provider=provider)

    app.test_client().get("/items/3")
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert entered, "the slow hook never ran"
    # Relational, not a budget: the span ended before the hook chain started,
    # so it is anchored to dispatch completion. A wall-clock ceiling here fails
    # on a loaded runner for reasons unrelated to the code, which is the class
    # of test this project keeps behind the `perf` marker.
    assert spans[0].end_time <= entered[0]


def test_head_to_streaming_endpoint_is_traced() -> None:
    # Hold: a HEAD request to a streaming route must still be traced. HEAD
    # sends no body (headers + empty terminal frame), so timing/status are
    # final at hook time — it must NOT be dropped as a live stream.
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from veloce import StreamingResponse
    from veloce.otel import instrument_with_otel

    exporter, provider = _exporter()

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/stream", name="stream")
    async def stream():
        async def gen():
            yield b"chunk"

        return StreamingResponse(gen())

    instrument_with_otel(app, tracer_provider=provider)
    resp = app.test_client().head("/stream")
    assert resp.status_code == 200

    spans = exporter.get_finished_spans()
    assert len(spans) == 1, "HEAD to a streaming endpoint must emit a span"
    assert spans[0].attributes["http.response.status_code"] == 200


def test_streaming_get_is_not_traced() -> None:
    # The complement: a real streaming GET body IS emitted later, so the
    # bridge correctly skips it (no misleading backdated span).
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from veloce import StreamingResponse
    from veloce.otel import instrument_with_otel

    exporter, provider = _exporter()

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/stream2", name="stream2")
    async def stream2():
        async def gen():
            yield b"chunk"

        return StreamingResponse(gen())

    instrument_with_otel(app, tracer_provider=provider)
    app.test_client().get("/stream2")
    assert exporter.get_finished_spans() == ()


def test_inbound_traceparent_continues_the_distributed_trace() -> None:
    # W3C context propagation: a request carrying a `traceparent` header must
    # produce a span that joins that trace — same trace_id, parented under the
    # inbound span id — rather than starting a fresh root.
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    exporter, app = _exporter_and_app()

    # A well-formed W3C traceparent: version-traceid-spanid-flags.
    trace_id_hex = "0af7651916cd43dd8448eb211c80319c"
    span_id_hex = "b7ad6b7169203331"
    traceparent = f"00-{trace_id_hex}-{span_id_hex}-01"

    app.test_client().get("/items/7", headers={"traceparent": traceparent})

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    # Same trace as the caller, and parented under the caller's span id.
    assert format(span.context.trace_id, "032x") == trace_id_hex
    assert span.parent is not None
    assert format(span.parent.span_id, "016x") == span_id_hex


def test_malformed_traceparent_does_not_raise_and_still_emits_a_span(monkeypatch) -> None:
    # A propagator that raises on a bad inbound `traceparent` (a custom propagator,
    # or a stricter future revision) must not abort span emission: the emit hook
    # swallows the error and falls back to a fresh root context. One span is still
    # recorded, rooted (no parent), and the request itself is unaffected.
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    import veloce.otel as otel

    class _RaisingPropagator(otel._W3CPropagator):
        def extract(self, carrier, *args, **kwargs):
            raise ValueError("malformed traceparent")

    monkeypatch.setattr(otel, "_W3CPropagator", _RaisingPropagator)
    exporter, app = _exporter_and_app()

    trace_id_hex = "0af7651916cd43dd8448eb211c80319c"
    span_id_hex = "b7ad6b7169203331"
    resp = app.test_client().get(
        "/items/7", headers={"traceparent": f"00-{trace_id_hex}-{span_id_hex}-01"}
    )

    assert resp.status_code == 200
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].parent is None


def test_no_traceparent_starts_a_fresh_root_span() -> None:
    # Without inbound trace headers the span is a clean root (no parent),
    # exactly as before propagation was added.
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    exporter, app = _exporter_and_app()
    app.test_client().get("/items/7")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].parent is None


def test_traceparent_continued_even_when_a_before_hook_short_circuits() -> None:
    # Regression: trace continuation must not depend on hook ordering. A
    # request rejected by an earlier `before_request` hook (auth/guard) still
    # emits a span, and that span must still join the inbound trace — the
    # bridge extracts the parent in its emit hook (which runs on every dispatch
    # path), not a skippable before_request hook.
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from veloce import Response

    exporter, app = _exporter_and_app()

    @app.before_request
    def _gate(request):
        # Short-circuit before the route handler ever runs.
        return Response(body=b"denied", status_code=403)

    trace_id_hex = "0af7651916cd43dd8448eb211c80319c"
    span_id_hex = "b7ad6b7169203331"
    app.test_client().get(
        "/items/7", headers={"traceparent": f"00-{trace_id_hex}-{span_id_hex}-01"}
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["http.response.status_code"] == 403
    # The span still joins the inbound trace despite the short-circuit.
    assert format(span.context.trace_id, "032x") == trace_id_hex
    assert span.parent is not None
    assert format(span.parent.span_id, "016x") == span_id_hex


# ── idempotency: a second instrument_with_otel is a no-op ──────────────


def test_re_instrument_warns_and_does_not_register_twice() -> None:
    """Calling the bridge twice on one app must not register a second hook —
    that would emit two server spans per request. The redundant call warns and
    returns the existing hook."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from veloce.otel import instrument_with_otel

    app = Veloce(openapi_url=None)
    first = instrument_with_otel(app)
    assert len(app.instrumentation_hooks) == 1

    with pytest.warns(RuntimeWarning, match="already called"):
        second = instrument_with_otel(app)

    # No duplicate hook, and the existing one is handed back unchanged.
    assert len(app.instrumentation_hooks) == 1
    assert second is first


def test_re_instrument_emits_a_single_span_per_request() -> None:
    """End-to-end proof of the idempotency guard: even after a redundant
    instrument call, exactly one span is exported per request."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from veloce.otel import instrument_with_otel

    exporter, provider, app = _traced_app()
    with pytest.warns(RuntimeWarning):
        instrument_with_otel(app, tracer_provider=provider)

    app.test_client().get("/items/7")
    assert len(exporter.get_finished_spans()) == 1


def test_two_apps_in_one_process_each_get_a_bridge() -> None:
    """The dedup state lives on the app, not a module global, so two distinct
    apps in one process each register their own bridge."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from veloce.otel import instrument_with_otel

    app_a = Veloce(openapi_url=None)
    app_b = Veloce(openapi_url=None)
    instrument_with_otel(app_a)
    instrument_with_otel(app_b)

    assert len(app_a.instrumentation_hooks) == 1
    assert len(app_b.instrumentation_hooks) == 1


# ── on_span enrichment callback ───────────────────────────────────────


def test_on_span_enriches_every_emitted_span() -> None:
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from veloce.otel import instrument_with_otel

    exporter, provider = _exporter()

    seen_routes: list[str | None] = []

    def enrich(span, metrics):
        seen_routes.append(metrics.route)
        span.set_attribute("app.tenant", "acme")

    app = _app()
    instrument_with_otel(app, tracer_provider=provider, on_span=enrich)

    app.test_client().get("/items/7")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["app.tenant"] == "acme"
    assert seen_routes == ["/items/{item_id}"]


def test_on_span_exception_is_suppressed_and_span_still_ends() -> None:
    """An on_span that raises must not break the response nor leak through the
    instrumentation hook; the span still ends and is exported."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from veloce.otel import instrument_with_otel

    exporter, provider = _exporter()

    def boom(span, metrics):
        raise RuntimeError("enrichment is broken")

    app = _app()
    instrument_with_otel(app, tracer_provider=provider, on_span=boom)

    resp = app.test_client().get("/items/7")
    assert resp.status_code == 200
    # The span was still emitted with its built-in attributes intact.
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["http.route"] == "/items/{item_id}"


# ── error.type attribute on raised 5xx ────────────────────────────────


def test_error_type_attribute_set_for_unhandled_exception() -> None:
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from opentelemetry.trace import StatusCode

    exporter, app = _exporter_and_app()
    app.test_client().get("/crash")  # raises ValueError -> 500

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["http.response.status_code"] == 500
    assert span.status.status_code == StatusCode.ERROR
    # The low-cardinality class name is exported, never the message.
    assert span.attributes["error.type"] == "ValueError"
    for value in span.attributes.values():
        assert "kaboom" not in str(value)


def test_no_error_type_attribute_for_success() -> None:
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    exporter, app = _exporter_and_app()
    app.test_client().get("/items/7")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "error.type" not in spans[0].attributes


# ── route-template exclusion threaded into the bridge ─────────────────


def test_exclude_routes_suppresses_spans_for_noisy_routes() -> None:
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from veloce.otel import instrument_with_otel

    exporter, provider = _exporter()

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/work")
    async def work():
        return {"done": True}

    instrument_with_otel(app, tracer_provider=provider, exclude_routes={"/health"})

    app.test_client().get("/health")
    app.test_client().get("/work")

    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert names == ["/work"]


# ── live mode: a real server span wraps handler execution ─────────────


def _live_exporter_and_app(**kwargs):
    """Build an in-memory exporter wired to a *live*-instrumented `_app()`."""
    return _traced_app(live=True, **kwargs)


def test_live_handler_span_is_child_of_server_span() -> None:
    """The core of live mode: a span created inside the handler parents under the
    live server span, with the route-template name and the standard attributes —
    something the backdated mode cannot do."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from opentelemetry.trace import SpanKind

    exporter, provider, app = _live_exporter_and_app()
    child_tracer = provider.get_tracer("handler")

    @app.get("/work", name="work")
    async def work():
        with child_tracer.start_as_current_span("inner-work"):
            pass
        return {"ok": True}

    app.test_client().get("/work")

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"/work", "inner-work"}
    server = spans["/work"]
    inner = spans["inner-work"]

    # The server span is a clean SERVER root; the handler span is its child.
    assert server.kind == SpanKind.SERVER
    assert server.parent is None
    assert inner.parent is not None
    assert inner.parent.span_id == server.context.span_id
    assert inner.context.trace_id == server.context.trace_id

    # The live span carries the same attribute set the backdated mode emits.
    assert server.attributes["http.request.method"] == "GET"
    assert server.attributes["http.route"] == "/work"
    assert server.attributes["http.response.status_code"] == 200
    assert "duration_ms" in server.attributes


def test_live_context_token_not_leaked_on_handler_exception() -> None:
    """The attach/detach must be balanced even when the handler raises: after the
    request the ambient OTel context is restored (no leaked token), and the
    server span is still ended and exported with the error recorded."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from opentelemetry import trace
    from opentelemetry.trace import StatusCode

    exporter, provider, app = _live_exporter_and_app()

    # Before the request there is no current recording span.
    assert not trace.get_current_span().is_recording()

    app.test_client().get("/crash")  # raises ValueError -> 500

    # After the request the token has been detached: the ambient context is back
    # to having no recording span. A leaked token would leave the server span (or
    # a stale context) current here.
    assert not trace.get_current_span().is_recording()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["http.response.status_code"] == 500
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "ValueError"


def test_live_concurrent_requests_get_isolated_parent_spans() -> None:
    """Under interleaved concurrent requests each handler span parents under its
    own request's server span — the per-request attach/detach token model is
    concurrency-safe and never cross-parents."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    import asyncio

    from veloce.testclient import AsyncTestClient

    exporter, provider, app = _live_exporter_and_app()
    child_tracer = provider.get_tracer("handler")

    @app.get("/a", name="a")
    async def a():
        with child_tracer.start_as_current_span("child-a"):
            await asyncio.sleep(0.02)
        return {"r": "a"}

    @app.get("/b", name="b")
    async def b():
        with child_tracer.start_as_current_span("child-b"):
            await asyncio.sleep(0.02)
        return {"r": "b"}

    async def drive():
        async with AsyncTestClient(app) as client:
            await asyncio.gather(client.get("/a"), client.get("/b"))

    asyncio.run(drive())

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"/a", "/b", "child-a", "child-b"}

    # Each child parents under its own request's server span, never the other's.
    assert spans["child-a"].parent.span_id == spans["/a"].context.span_id
    assert spans["child-b"].parent.span_id == spans["/b"].context.span_id
    assert spans["child-a"].parent.span_id != spans["/b"].context.span_id
    assert spans["/a"].context.trace_id != spans["/b"].context.trace_id


def test_live_continues_inbound_distributed_trace() -> None:
    """A request carrying a W3C traceparent produces a live server span that
    joins that trace (same trace_id, parented under the caller's span)."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    exporter, provider, app = _live_exporter_and_app()

    trace_id_hex = "0af7651916cd43dd8448eb211c80319c"
    span_id_hex = "b7ad6b7169203331"
    traceparent = f"00-{trace_id_hex}-{span_id_hex}-01"

    app.test_client().get("/items/7", headers={"traceparent": traceparent})

    spans = [s for s in exporter.get_finished_spans() if s.name == "/items/{item_id}"]
    assert len(spans) == 1
    span = spans[0]
    assert format(span.context.trace_id, "032x") == trace_id_hex
    assert span.parent is not None
    assert format(span.parent.span_id, "016x") == span_id_hex


def test_live_no_traceparent_starts_a_fresh_root_span() -> None:
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    exporter, provider, app = _live_exporter_and_app()
    app.test_client().get("/items/7")

    spans = [s for s in exporter.get_finished_spans() if s.name == "/items/{item_id}"]
    assert len(spans) == 1
    assert spans[0].parent is None


def test_live_streaming_response_is_traced_end_to_end() -> None:
    """Unlike the backdated mode (which skips streamed records), live mode times a
    streaming body end to end: the span ends only after the body drains, so its
    window covers the whole stream."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    import asyncio

    from veloce.http.response import StreamingResponse

    exporter, provider, app = _live_exporter_and_app()

    @app.get("/slow-stream", name="slow_stream")
    async def slow_stream():
        async def gen():
            for i in range(3):
                await asyncio.sleep(0.02)
                yield f"chunk-{i}".encode()

        return StreamingResponse(gen())

    resp = app.test_client().get("/slow-stream")
    assert resp.status_code == 200
    assert resp.body == b"chunk-0chunk-1chunk-2"

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "/slow-stream"
    # The span window spans the full ~60ms drain, not just response production.
    duration_ns = span.end_time - span.start_time
    assert duration_ns >= 50_000_000


def test_live_on_span_enriches_the_live_span() -> None:
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    def enrich(span, metrics):
        span.set_attribute("app.tenant", "acme")

    exporter, provider, app = _live_exporter_and_app(on_span=enrich)

    app.test_client().get("/items/7")

    spans = [s for s in exporter.get_finished_spans() if s.name == "/items/{item_id}"]
    assert len(spans) == 1
    assert spans[0].attributes["app.tenant"] == "acme"


def test_live_is_idempotent_no_duplicate_wrapper_or_hook() -> None:
    """A second live install must not stack a second ASGI wrapper or a second
    enrichment hook (which would double the per-request spans)."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    exporter, provider, app = _live_exporter_and_app()
    from veloce.otel import instrument_with_otel

    with pytest.warns(RuntimeWarning, match="already called"):
        instrument_with_otel(app, tracer_provider=provider, live=True)

    assert len(app.instrumentation_hooks) == 1
    # The live span wrapper is a single PH_ASGI_WRAP feature, not an entry in the
    # raw `_asgi_middleware` list; a second install must not register a duplicate.
    otel_specs = [spec for spec in app._features if spec.name == "otel.live_span"]
    assert len(otel_specs) == 1
    assert app._asgi_middleware == []

    app.test_client().get("/items/7")
    request_spans = [s for s in exporter.get_finished_spans() if s.name == "/items/{item_id}"]
    assert len(request_spans) == 1


def test_live_unmatched_request_uses_method_fallback_name() -> None:
    """A 404 has no matched route: the live span keeps the stable method-based
    fallback name (set at start, not overwritten) and carries no http.route."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    exporter, provider, app = _live_exporter_and_app()

    secret = "/no/such/path-unique-marker-9f3a"
    resp = app.test_client().get(secret)
    assert resp.status_code == 404

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "HTTP GET"
    assert "http.route" not in span.attributes
    for value in span.attributes.values():
        assert value != secret


def test_live_span_middleware_installed_outermost() -> None:
    """Live instrumentation must wrap pre-existing ASGI middleware: the live span
    wrapper leads the ASGI list (outermost - the list is wrapped in reverse) even
    when an ASGI middleware was registered before `instrument_with_otel(live=True)`."""
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    from opentelemetry.sdk.trace import TracerProvider

    from veloce.otel import instrument_with_otel

    class _ProbeASGI:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            await self.app(scope, receive, send)

    from veloce._pipeline import flatten_asgi_wrap

    app = _app()
    app.add_middleware(_ProbeASGI)  # user ASGI middleware registered first
    instrument_with_otel(app, tracer_provider=TracerProvider(), live=True)

    # The compiled PH_ASGI_WRAP slot orders the live span ahead of any standard
    # ASGI middleware (it carries the higher `order`), so it leads the flattened
    # chain - index 0 is wrapped last and therefore outermost.
    cp = app._ensure_pipeline()
    classes = [cls for cls, _opts in flatten_asgi_wrap(cp.asgi_wrap)]
    assert classes[0].__name__ == "_LiveSpanMiddleware"
    assert _ProbeASGI in classes


def test_live_span_strictly_nests_entire_request() -> None:
    """Step 5 regression: the live server span must wrap the ENTIRE request.

    With the live wrapper routed through PH_ASGI_WRAP it must still compose
    outermost - its span must open before any inner ASGI middleware and the
    dispatch run, and close only after they finish. An inner marker ASGI
    middleware records the wall-clock instants it enters and exits around the
    dispatch; the exported server span's start must precede the marker's enter
    and its end must follow the marker's exit, proving strict nesting.
    """
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")

    import time

    from veloce.otel import instrument_with_otel

    events: dict[str, int] = {}

    class _MarkerASGI:
        """Inner ASGI middleware that timestamps its own enter/exit."""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            events["marker_enter"] = time.time_ns()
            await self.app(scope, receive, send)
            events["marker_exit"] = time.time_ns()

    exporter, provider = _exporter()

    app = _app()
    # Register the inner marker BEFORE the live span: under correct PH_ASGI_WRAP
    # ordering the live span must still end up outermost regardless.
    app.add_middleware(_MarkerASGI)
    instrument_with_otel(app, tracer_provider=provider, live=True)

    @app.get("/nested", name="nested")
    async def nested():
        events["dispatch"] = time.time_ns()
        return {"ok": True}

    resp = app.test_client().get("/nested")
    assert resp.status_code == 200

    # The marker and the dispatch all ran, and the dispatch ran between the
    # marker's enter and exit (the marker is inside the live span, around dispatch).
    assert {"marker_enter", "dispatch", "marker_exit"} <= set(events)
    assert events["marker_enter"] <= events["dispatch"] <= events["marker_exit"]

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    # Strict nesting: the live span opened before the inner marker entered and
    # closed only after the marker exited - so it wraps the whole request.
    assert span.start_time <= events["marker_enter"]
    assert span.end_time >= events["marker_exit"]
