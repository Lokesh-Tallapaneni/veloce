"""End-to-end middleware behaviour exercised through the real TestClient flow.

Each test drives a `Veloce` app via `TestClient` so the request goes through
the full ASGI pipeline (request middleware -> match -> handler -> response
middleware). These are intentionally NOT unit tests of the middleware in
isolation -- the value is in catching wiring regressions where a fix to the
middleware itself doesn't surface end-to-end.
"""

from __future__ import annotations

import logging

import orjson
import pytest

from tests.conftest import make_request
from veloce import (
    CORSMiddleware,
    JSONResponse,
    LoggingMiddleware,
    ProxyFix,
    Request,
    TestClient,
    Veloce,
)

# ── Fixture: keep the global `veloce.access` logger from leaking across tests ─


@pytest.fixture
def access_logger_state():
    """Snapshot and restore the `veloce.access` logger across the test."""
    logger = logging.getLogger("veloce.access")
    saved_level = logger.level
    saved_handlers = list(logger.handlers)
    saved_propagate = logger.propagate
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    try:
        yield logger
    finally:
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


# ── App factories ────────────────────────────────────────────────────────


def _proxy_app(**kwargs) -> Veloce:
    """App that echoes `script_root` so we can observe ProxyFix's effect."""
    app = Veloce(openapi_url=None)
    app.add_middleware(ProxyFix(**kwargs))

    @app.get("/info")
    async def info(request: Request):
        return {"script_root": request.script_root, "host": request.host}

    return app


def _cors_app(**kwargs) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(CORSMiddleware(**kwargs))

    @app.get("/ping")
    async def ping(request: Request):
        return {"ok": True}

    return app


# ── 1-3: ProxyFix prefix selection ────────────────────────────────────────


def test_proxyfix_forwarded_prefix_sets_script_root():
    """`Forwarded: prefix=/api` with no XFP -> request.script_root == '/api'."""
    app = _proxy_app(x_prefix=1)
    client = TestClient(app)
    resp = client.get("/info", headers={"Forwarded": "for=192.0.2.1; prefix=/api"})
    assert resp.status_code == 200
    assert resp.json()["script_root"] == "/api"


def test_proxyfix_x_forwarded_prefix_only_sets_script_root():
    """`X-Forwarded-Prefix: /v2` alone is honoured."""
    app = _proxy_app(x_prefix=1)
    client = TestClient(app)
    resp = client.get("/info", headers={"X-Forwarded-Prefix": "/v2"})
    assert resp.status_code == 200
    assert resp.json()["script_root"] == "/v2"


def test_proxyfix_forwarded_wins_over_x_forwarded_prefix():
    """RFC 7239 `Forwarded` takes precedence over the legacy header."""
    app = _proxy_app(x_prefix=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={
            "Forwarded": "for=192.0.2.1; prefix=/api",
            "X-Forwarded-Prefix": "/legacy",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["script_root"] == "/api"


# ── 4-5: CRLF rejection in proxy-supplied values ──────────────────────────
#
# The middleware raises `ValueError` at the injection point. With the default
# (non-debug, non-propagating) app, the dispatcher converts the unhandled
# exception into a generic 500 -- the contract we care about is that no CRLF
# sequence ever lands on a response header.


def _assert_no_header_injection(resp) -> None:
    """No response header may contain a leaked `Injected` field."""
    for name, value in resp.raw_headers:
        assert b"Injected" not in name, f"injected header name leaked: {name!r}"
        assert b"Injected" not in value, f"injected header value leaked: {value!r}"
        assert b"\r\n" not in value, f"CRLF leaked into header value: {value!r}"


def test_proxyfix_crlf_in_x_forwarded_prefix_rejected_cleanly():
    """`X-Forwarded-Prefix` carrying CRLF must not poison the response."""
    app = _proxy_app(x_prefix=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"X-Forwarded-Prefix": "/api\r\nInjected: 1"},
    )
    assert resp.status_code == 500
    _assert_no_header_injection(resp)


def test_proxyfix_crlf_in_x_forwarded_host_rejected_cleanly():
    """`X-Forwarded-Host` carrying CRLF gets the same treatment."""
    app = _proxy_app(x_host=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"X-Forwarded-Host": "evil.example.com\r\nInjected: 1"},
    )
    assert resp.status_code == 500
    _assert_no_header_injection(resp)


# ── 6: CORS bad regex -> ValueError at construction ───────────────────────


def test_cors_invalid_regex_raises_value_error():
    """A malformed `allow_origin_regex` must fail loudly with the bad pattern."""
    with pytest.raises(ValueError, match=r"\["):
        CORSMiddleware(allow_origin_regex="[")


# ── 7: CORS preflight with wildcard ───────────────────────────────────────


def test_cors_preflight_wildcard_returns_expected_headers():
    """Preflight against `allow_origins=['*']` returns 204 with the wildcard."""
    app = _cors_app(allow_origins=["*"])
    client = TestClient(app)
    resp = client.options(
        "/ping",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Custom",
        },
    )
    assert resp.status_code == 204
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "GET" in resp.headers["Access-Control-Allow-Methods"]
    assert resp.headers["Access-Control-Max-Age"] == "600"
    # Wildcard allow_headers echoes whatever the client asked for.
    assert resp.headers["Access-Control-Allow-Headers"] == "X-Custom"


# ── 8: Logging level preserved through end-to-end app construction ────────


def test_logging_middleware_preserves_preconfigured_level(access_logger_state):
    """Pre-set WARNING + NullHandler on `veloce.access` must survive
    middleware instantiation when the middleware is wired into a real app."""
    logger = access_logger_state
    pre_handler = logging.NullHandler()
    logger.addHandler(pre_handler)
    logger.setLevel(logging.WARNING)

    app = Veloce(openapi_url=None)
    app.add_middleware(LoggingMiddleware())

    assert logger.level == logging.WARNING
    assert logger.handlers == [pre_handler]

    # And the wired-up app still serves traffic without re-configuring the logger.
    @app.get("/healthz")
    async def healthz(request: Request):
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert logger.level == logging.WARNING
    assert logger.handlers == [pre_handler]


class TestMiddlewareHTTPDecorator:
    """Test @app.middleware('http') with the call_next pattern."""

    async def test_middleware_http_modifies_response(self):
        app = Veloce(openapi_url=None)

        @app.middleware("http")
        async def add_timing(request: Request, call_next):
            response = await call_next(request)
            response.headers["X-Process"] = "true"
            response._encoded = None
            return response

        @app.get("/data")
        async def data(request: Request):
            return {"value": 42}

        resp = await app.handle_request(make_request(path="/data"))
        assert resp.status_code == 200
        assert resp.headers.get("X-Process") == "true"
        assert orjson.loads(resp.body)["value"] == 42

    async def test_middleware_http_short_circuit(self):
        app = Veloce(openapi_url=None)

        @app.middleware("http")
        async def block_everything(request: Request, call_next):
            if request.path == "/blocked":
                return JSONResponse({"error": "blocked"}, status_code=403)
            return await call_next(request)

        @app.get("/blocked")
        async def blocked(request: Request):
            return {"should_not": "reach"}

        @app.get("/allowed")
        async def allowed(request: Request):
            return {"ok": True}

        resp = await app.handle_request(make_request(path="/blocked"))
        assert resp.status_code == 403

        resp = await app.handle_request(make_request(path="/allowed"))
        assert resp.status_code == 200

    async def test_multiple_http_middleware_chain(self):
        app = Veloce(openapi_url=None)
        order = []

        @app.middleware("http")
        async def mw1(request: Request, call_next):
            order.append("mw1_before")
            response = await call_next(request)
            order.append("mw1_after")
            return response

        @app.middleware("http")
        async def mw2(request: Request, call_next):
            order.append("mw2_before")
            response = await call_next(request)
            order.append("mw2_after")
            return response

        @app.get("/chain")
        async def chain(request: Request):
            order.append("handler")
            return {"ok": True}

        await app.handle_request(make_request(path="/chain"))
        assert order == ["mw1_before", "mw2_before", "handler", "mw2_after", "mw1_after"]
