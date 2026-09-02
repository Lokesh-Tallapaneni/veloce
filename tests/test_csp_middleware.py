"""Tests for CSPMiddleware and csp_nonce."""

from __future__ import annotations

import pytest

import veloce
from veloce import CSPMiddleware, Response, TestClient, Veloce, csp_nonce


def test_static_policy_verbatim():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(policy="default-src 'self'"))

    @app.get("/")
    async def index(request):
        return Response(body=b"x")

    r = TestClient(app).get("/")
    assert r.headers.get("Content-Security-Policy") == "default-src 'self'"


def test_nonce_template():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(policy="script-src 'self' {nonce}"))
    seen = {}

    @app.get("/")
    async def index(request):
        seen["nonce"] = csp_nonce(request)
        return Response(body=b"x")

    r = TestClient(app).get("/")
    csp = r.headers["Content-Security-Policy"]
    assert "'nonce-" in csp
    assert f"'nonce-{seen['nonce']}'" in csp


def test_per_request_uniqueness():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(policy="script-src {nonce}"))

    @app.get("/")
    async def index(request):
        return Response(body=b"x")

    client = TestClient(app)
    a = client.get("/").headers["Content-Security-Policy"]
    b = client.get("/").headers["Content-Security-Policy"]
    assert a != b


def test_dict_policy_form():
    app = Veloce(openapi_url=None)
    app.add_middleware(
        CSPMiddleware(policy={"default-src": "'self'", "script-src": ["'self'", "'nonce'"]})
    )

    @app.get("/")
    async def index(request):
        return Response(body=b"x")

    csp = TestClient(app).get("/").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self' 'nonce-" in csp


def test_report_only_independence():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(report_only_policy="default-src 'self'"))

    @app.get("/")
    async def index(request):
        return Response(body=b"x")

    r = TestClient(app).get("/")
    assert r.headers.get("Content-Security-Policy-Report-Only") == "default-src 'self'"
    # Enforce header must be absent.
    assert not any(k.lower() == "content-security-policy" for k in r.headers)


def test_both_headers():
    app = Veloce(openapi_url=None)
    app.add_middleware(
        CSPMiddleware(policy="default-src 'self'", report_only_policy="img-src 'self'")
    )

    @app.get("/")
    async def index(request):
        return Response(body=b"x")

    r = TestClient(app).get("/")
    assert r.headers.get("Content-Security-Policy") == "default-src 'self'"
    assert r.headers.get("Content-Security-Policy-Report-Only") == "img-src 'self'"


def test_no_clobber():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(policy="default-src 'self'"))

    @app.get("/")
    async def index(request):
        return Response(body=b"x", headers={"Content-Security-Policy": "custom"})

    r = TestClient(app).get("/")
    assert r.headers.get("Content-Security-Policy") == "custom"


def test_no_clobber_lowercase_override():
    """A lowercase route override must suppress the default (RFC 9110 §5.1).

    Header field names are case-insensitive; emitting a second CSP header would
    make browsers intersect the two policies, silently narrowing the route's
    intended policy.
    """
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(policy="default-src 'self'"))

    @app.get("/")
    async def index(request):
        return Response(body=b"x", headers={"content-security-policy": "custom"})

    r = TestClient(app).get("/")
    csp_headers = [
        v.decode("latin-1")
        for k, v in r.raw_headers
        if k.decode("latin-1").lower() == "content-security-policy"
    ]
    assert csp_headers == ["custom"]


def test_report_only_no_clobber_lowercase_override():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(report_only_policy="default-src 'self'"))

    @app.get("/")
    async def index(request):
        return Response(
            body=b"x",
            headers={"content-security-policy-report-only": "custom"},
        )

    r = TestClient(app).get("/")
    csp_headers = [
        v.decode("latin-1")
        for k, v in r.raw_headers
        if k.decode("latin-1").lower() == "content-security-policy-report-only"
    ]
    assert csp_headers == ["custom"]


def test_lazy_materialization_without_handler_read():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(policy="script-src {nonce}"))

    @app.get("/")
    async def index(request):
        return Response(body=b"x")

    r = TestClient(app).get("/")
    assert "'nonce-" in r.headers["Content-Security-Policy"]


def test_a_non_string_policy_is_a_type_error():
    # The empty-configuration refusal is next door in
    # `test_csp_refusal_under_optimisation.py`, which also covers the part that
    # matters about it - that it is a `ValueError` rather than an `assert`, so
    # `python -O` cannot strip it. Asserting it here too said less and said it
    # twice.
    with pytest.raises(TypeError):
        CSPMiddleware(policy=123)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"policy": "script-src 'self' {nonce}"},
        # A directive mapping whose 'nonce' source normalizes to {nonce}.
        {"policy": {"script-src": ["'self'", "'nonce'"]}},
        {"report_only_policy": "script-src {nonce}"},
    ],
    ids=["string-policy", "directive-mapping", "report-only"],
)
def test_nonce_disabled_with_placeholder_rejected(kwargs):
    # A template still referencing a nonce while nonce generation is off would
    # render 'nonce-None' (a real but wrong nonce to browsers). Construction
    # must fail fast instead of emitting the misleading header. Parametrized so
    # the first rejected shape failing does not hide the other two.
    with pytest.raises(ValueError):
        CSPMiddleware(nonce=False, **kwargs)


def test_a_nonce_free_policy_with_nonce_disabled_is_accepted():
    """The control: the guard must not reject a policy with no placeholder."""
    CSPMiddleware(policy="default-src 'self'", nonce=False)


def test_public_import():
    """The names are re-exported from the package root, not only from the
    middleware module. The body used to be the import alone, which this module
    already performs at the top - so it could not have failed here without
    failing collection first."""

    assert veloce.CSPMiddleware is CSPMiddleware
    assert veloce.csp_nonce is csp_nonce
    assert "CSPMiddleware" in veloce.__all__
    assert "csp_nonce" in veloce.__all__


# ── a short-circuited request must still get a real nonce ────────────
#
# `process_response` interpolated `csp_nonce(request)` unconditionally. When an
# earlier middleware answers from `process_request` - an unlisted Host, a rate
# limit, an auth gate - CSP's own `process_request` never ran, no nonce was
# armed, and the header shipped the fixed token `nonce-None`. A browser parses
# that as a real nonce, so an injected `<script nonce="None">` executes while
# the page's own inline script (carrying no nonce) is blocked: the policy is
# inverted rather than merely weakened. The constructor already refuses a
# policy that would render `nonce-None`; the response path did it anyway.


def _nonce_middleware() -> CSPMiddleware:
    return CSPMiddleware(policy={"default-src": "'self'", "script-src": "'nonce'"})


def _bare_request():
    return veloce.Request(
        method="GET", path="/", query_string="", headers=[("host", "x")], body=b""
    )


async def test_a_short_circuited_response_never_ships_the_none_nonce():
    """NEGATIVE: the fixed token must not reach the header."""
    middleware = _nonce_middleware()
    request = _bare_request()

    response = await middleware.process_response(request, Response("gate"))

    policy = response.headers["Content-Security-Policy"]
    assert "nonce-None" not in policy
    assert "'nonce-" in policy


async def test_a_short_circuited_nonce_is_unguessable_and_per_response():
    """NEGATIVE: minting one is only a fix if it differs every time."""
    middleware = _nonce_middleware()

    first = await middleware.process_response(_bare_request(), Response("gate"))
    second = await middleware.process_response(_bare_request(), Response("gate"))

    assert first.headers["Content-Security-Policy"] != second.headers["Content-Security-Policy"]


async def test_a_minted_nonce_is_readable_through_csp_nonce():
    """POSITIVE: the value in the header is the one the request carries.

    Storing it back keeps `csp_nonce(request)` and the emitted policy in
    agreement, so anything reading it later sees the nonce that was sent.
    """
    middleware = _nonce_middleware()
    request = _bare_request()

    response = await middleware.process_response(request, Response("gate"))

    minted = csp_nonce(request)
    # Not just "the header agrees with the state": pre-fix both were the string
    # `None`, so agreement alone passes vacuously.
    assert minted is not None
    assert minted != "None"
    assert f"'nonce-{minted}'" in response.headers["Content-Security-Policy"]


async def test_the_ordinary_request_path_is_unchanged():
    """POSITIVE: an armed nonce must still be the one emitted."""
    middleware = _nonce_middleware()
    request = _bare_request()

    await middleware.process_request(request)
    armed = csp_nonce(request)
    response = await middleware.process_response(request, Response("ok"))

    assert f"'nonce-{armed}'" in response.headers["Content-Security-Policy"]
