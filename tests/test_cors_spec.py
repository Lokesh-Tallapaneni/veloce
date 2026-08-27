"""CORS spec-compliance tests (M4)."""

from __future__ import annotations

import inspect
import textwrap

import pytest

from veloce import CORSMiddleware, Middleware, Veloce
from veloce.http.header_set import HeaderSet
from veloce.http.response import Response
from veloce.testclient import TestClient

# ── Construction validation ────────────────────────────────────────────


def test_wildcard_with_credentials_rejected_at_construction():
    """`Access-Control-Allow-Origin: *` + credentials is forbidden by the
    Fetch CORS spec — construction must fail loudly."""
    with pytest.raises(ValueError):
        CORSMiddleware(allow_origins=["*"], allow_credentials=True)


def test_wildcard_headers_with_credentials_rejected():
    with pytest.raises(ValueError):
        CORSMiddleware(
            allow_origins=["http://x.com"],
            allow_headers=["*"],
            allow_credentials=True,
        )


def test_invalid_allow_origin_regex_wrapped_as_value_error():
    """A bad regex must surface as ValueError mentioning the offending
    pattern — not a cryptic stdlib `re.error` traceback."""
    bad = "["
    with pytest.raises(ValueError, match=r"allow_origin_regex"):
        CORSMiddleware(allow_origin_regex=bad)
    with pytest.raises(ValueError) as exc_info:
        CORSMiddleware(allow_origin_regex=bad)
    assert repr(bad) in str(exc_info.value)


# ── Allow-origin resolution ────────────────────────────────────────────


def _make_app(**kwargs) -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CORSMiddleware(**kwargs))

    @app.get("/x")
    async def x():
        return {"ok": True}

    return app


def test_wildcard_returns_star_when_credentials_off():
    client = TestClient(_make_app(allow_origins=["*"]))
    resp = client.get("/x", headers={"origin": "http://anyone.example"})
    assert resp.headers.get("access-control-allow-origin") == "*"


def test_credentials_echoes_exact_origin_never_star():
    # `allow_headers=["*"]` is forbidden with credentials, so be explicit.
    client = TestClient(
        _make_app(
            allow_origins=["http://allowed.example"],
            allow_headers=["Content-Type", "Authorization"],
            allow_credentials=True,
        )
    )
    resp = client.get("/x", headers={"origin": "http://allowed.example"})
    assert resp.headers.get("access-control-allow-origin") == "http://allowed.example"
    assert resp.headers.get("access-control-allow-credentials") == "true"


def test_disallowed_origin_gets_no_allow_origin_header():
    client = TestClient(_make_app(allow_origins=["http://allowed.example"]))
    resp = client.get("/x", headers={"origin": "http://evil.example"})
    assert resp.headers.get("access-control-allow-origin") is None


def test_regex_matches_subdomain_pattern():
    # No `*` in allow_origins — only the regex gates membership.
    client = TestClient(_make_app(allow_origins=[], allow_origin_regex=r"https://.*\.example\.com"))
    resp = client.get("/x", headers={"origin": "https://api.example.com"})
    assert resp.headers.get("access-control-allow-origin") == "https://api.example.com"

    resp2 = client.get("/x", headers={"origin": "https://example.com"})
    # Not a subdomain match — must be denied.
    assert resp2.headers.get("access-control-allow-origin") is None


def test_regex_only_config_does_not_default_to_wildcard():
    """`allow_origin_regex` with no explicit `allow_origins` must gate by the
    regex, not fall back to the `*` default and echo `*` to every origin."""
    client = TestClient(_make_app(allow_origin_regex=r"https://app\.example\.com"))

    resp = client.get("/x", headers={"origin": "https://app.example.com"})
    assert resp.headers.get("access-control-allow-origin") == "https://app.example.com"

    hostile = client.get("/x", headers={"origin": "https://evil.example"})
    assert hostile.headers.get("access-control-allow-origin") is None


# ── Vary: Origin ───────────────────────────────────────────────────────


def test_vary_origin_emitted_when_origin_echoed():
    """Whenever ACAO depends on the request origin, Vary: Origin is required
    to prevent cache poisoning across origins."""
    client = TestClient(_make_app(allow_origins=["http://a.example", "http://b.example"]))
    resp = client.get("/x", headers={"origin": "http://a.example"})
    assert "Origin" in resp.headers.get("vary", "")


def test_vary_origin_not_added_for_pure_wildcard():
    """`*` without credentials is origin-agnostic; Vary: Origin is unneeded."""
    client = TestClient(_make_app(allow_origins=["*"]))
    resp = client.get("/x", headers={"origin": "http://anywhere.example"})
    # `*` mode: no need for Vary on Origin.
    vary = resp.headers.get("vary", "")
    assert "Origin" not in vary


def test_vary_appended_not_replaced():
    """A pre-existing Vary value must be preserved when we add Origin."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CORSMiddleware(allow_origins=["http://a.example"]))

    @app.get("/x")
    async def x():

        return Response(body=b"ok", content_type="text/plain", headers={"Vary": "Accept"})

    resp = TestClient(app).get("/x", headers={"origin": "http://a.example"})
    vary = resp.headers.get("vary", "")
    assert "Accept" in vary
    assert "Origin" in vary


# ── Preflight (OPTIONS) ────────────────────────────────────────────────


def test_preflight_with_acrm_echoes_method_and_filtered_headers():
    client = TestClient(
        _make_app(
            allow_origins=["http://a.example"],
            allow_headers=["X-Custom", "Authorization"],
        )
    )
    resp = client.options(
        "/x",
        headers={
            "origin": "http://a.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "X-Custom, X-Disallowed",
        },
    )
    assert resp.status_code == 204
    # Only the allowed header from the requested set echoes back.
    allowed = resp.headers.get("access-control-allow-headers", "")
    assert "X-Custom" in allowed
    assert "X-Disallowed" not in allowed


def test_preflight_with_wildcard_headers_echoes_requested():
    """allow_headers=['*'] + a specific request-headers list → echo back
    exactly what was requested."""
    client = TestClient(_make_app(allow_origins=["http://a.example"], allow_headers=["*"]))
    resp = client.options(
        "/x",
        headers={
            "origin": "http://a.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "X-One, X-Two",
        },
    )
    assert resp.headers.get("access-control-allow-headers") == "X-One, X-Two"


def test_preflight_from_disallowed_origin_is_rejected():
    """A preflight from a disallowed origin gets a diagnostic 400 and no
    Access-Control-Allow-* headers."""
    client = TestClient(_make_app(allow_origins=["http://a.example"]))
    resp = client.options(
        "/x",
        headers={
            "origin": "http://evil.example",
            "access-control-request-method": "POST",
        },
    )
    assert resp.status_code == 400
    assert resp.headers.get("access-control-allow-origin") is None


def test_preflight_with_disallowed_method_is_rejected():
    """The preflight is for the actual request's method (carried in
    Access-Control-Request-Method); a method outside the allow-set is a
    rejection surfaced as a diagnostic 400."""
    client = TestClient(
        _make_app(allow_origins=["http://a.example"], allow_methods=["GET", "POST"])
    )
    resp = client.options(
        "/x",
        headers={
            "origin": "http://a.example",
            "access-control-request-method": "DELETE",
        },
    )
    assert resp.status_code == 400
    assert b"method" in resp.body
    # No allow-methods negotiation is emitted on a rejected preflight.
    assert resp.headers.get("access-control-allow-methods") is None


def test_preflight_with_allowed_method_succeeds():
    client = TestClient(
        _make_app(allow_origins=["http://a.example"], allow_methods=["GET", "POST"])
    )
    resp = client.options(
        "/x",
        headers={
            "origin": "http://a.example",
            "access-control-request-method": "POST",
        },
    )
    assert resp.status_code == 204
    assert resp.headers.get("access-control-allow-origin") == "http://a.example"


def test_options_without_request_method_skips_method_check():
    """A soft OPTIONS probe (Origin but no Access-Control-Request-Method)
    must not be rejected on the method check."""
    client = TestClient(_make_app(allow_origins=["http://a.example"], allow_methods=["GET"]))
    resp = client.options("/x", headers={"origin": "http://a.example"})
    assert resp.status_code == 204


def test_options_without_origin_passes_through():
    """An OPTIONS request with no Origin isn't CORS — the middleware should
    not synthesise a preflight response."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CORSMiddleware(allow_origins=["*"]))

    @app.options("/x")
    async def x():
        return {"reached_handler": True}

    resp = TestClient(app).options("/x")
    assert resp.json() == {"reached_handler": True}


# ── Expose-headers + max-age ───────────────────────────────────────────


def test_expose_headers_emitted():
    client = TestClient(_make_app(allow_origins=["*"], expose_headers=["X-Total", "X-Rate"]))
    resp = client.get("/x", headers={"origin": "http://any.example"})
    expose = resp.headers.get("access-control-expose-headers", "")
    assert "X-Total" in expose and "X-Rate" in expose


def test_preflight_emits_max_age():
    client = TestClient(_make_app(allow_origins=["*"], max_age=86400))
    resp = client.options(
        "/x",
        headers={"origin": "http://any.example", "access-control-request-method": "GET"},
    )
    assert resp.headers.get("access-control-max-age") == "86400"


# ── Private Network Access ─────────────────────────────────────────────


def test_pna_echoed_when_allowed_and_requested():
    """allow_private_network=True + a preflight requesting it → echo
    Access-Control-Allow-Private-Network: true."""
    client = TestClient(_make_app(allow_origins=["http://a.example"], allow_private_network=True))
    resp = client.options(
        "/x",
        headers={
            "origin": "http://a.example",
            "access-control-request-method": "GET",
            "access-control-request-private-network": "true",
        },
    )
    assert resp.status_code == 204
    assert resp.headers.get("access-control-allow-private-network") == "true"


def test_pna_not_echoed_when_not_configured():
    """Without opt-in, the grant header is never emitted even if requested."""
    client = TestClient(_make_app(allow_origins=["http://a.example"]))
    resp = client.options(
        "/x",
        headers={
            "origin": "http://a.example",
            "access-control-request-method": "GET",
            "access-control-request-private-network": "true",
        },
    )
    assert resp.status_code == 204
    assert resp.headers.get("access-control-allow-private-network") is None


def test_pna_not_echoed_when_not_requested():
    """Opt-in configured but the preflight does not request PNA → no grant."""
    client = TestClient(_make_app(allow_origins=["http://a.example"], allow_private_network=True))
    resp = client.options(
        "/x",
        headers={
            "origin": "http://a.example",
            "access-control-request-method": "GET",
        },
    )
    assert resp.status_code == 204
    assert resp.headers.get("access-control-allow-private-network") is None


# ── Wildcard regex + credentials ──────────────────────────────────────


@pytest.mark.parametrize(
    "pattern",
    [
        ".*",
        ".+",
        "^.*$",
        "^.+$",
        ".{0,}",
        ".{1,}",
        "^.{1,}$",
        # Bypasses the literal denylist; caught by the probe-test.
        r"[\s\S]*",
        r"(?s).*",
        r"(?:.|\n)*",
    ],
)
def test_cors_rejects_wildcard_regex_with_credentials(pattern):
    """R1 #58: a trivially-wildcard regex with credentials is the same
    security mistake as `allow_origins=["*"]` with credentials and must
    fail at construction with the same diagnostic."""
    with pytest.raises(ValueError, match=r"allow_credentials=True"):
        CORSMiddleware(allow_origin_regex=pattern, allow_credentials=True)


def test_cors_allows_wildcard_regex_without_credentials():
    """The same wildcard is fine when credentials are off — no echo-any
    risk because the response is `*` only without credentials."""
    mw = CORSMiddleware(allow_origin_regex=".*", allow_credentials=False)
    assert mw.allow_origin_regex is not None


def test_cors_allows_specific_regex_with_credentials():
    """A bounded regex remains valid with credentials — only trivially
    universal patterns are rejected. `allow_origins` is set explicitly
    so the default `["*"]` doesn't trip the separate wildcard-origins
    guard."""
    mw = CORSMiddleware(
        allow_origins=["https://app.example.com"],
        allow_origin_regex=r"https://[a-z]+\.example\.com",
        allow_headers=["Content-Type"],
        allow_credentials=True,
    )
    assert mw.allow_origin_regex is not None


def test_preflight_accepts_lowercase_configured_methods():
    """A lower-cased `allow_methods` config still passes a real browser preflight
    (browsers send the requested method in canonical case)."""
    client = TestClient(
        _make_app(allow_origins=["http://a.example"], allow_methods=["get", "post"])
    )
    resp = client.options(
        "/x",
        headers={"origin": "http://a.example", "access-control-request-method": "GET"},
    )
    assert resp.status_code == 204


def test_preflight_wildcard_methods_allows_any():
    """`allow_methods=['*']` accepts any requested method on preflight."""
    client = TestClient(_make_app(allow_origins=["http://a.example"], allow_methods=["*"]))
    resp = client.options(
        "/x",
        headers={"origin": "http://a.example", "access-control-request-method": "DELETE"},
    )
    assert resp.status_code == 204


# ── The qualifier headers require an allowed origin ──────────────────


def _qualified_app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(
        CORSMiddleware(
            allow_origins=["https://good.test"],
            allow_headers=["Content-Type"],
            allow_credentials=True,
            expose_headers=["X-Total"],
        )
    )

    @app.get("/")
    async def index():
        return {"ok": True}

    return app


def _has(response, name: str) -> bool:
    return name in {key.lower() for key in response.headers}


def test_an_allowed_origin_gets_both_qualifiers():
    with TestClient(_qualified_app()) as client:
        response = client.get("/", headers={"Origin": "https://good.test"})
    assert response.headers["access-control-allow-origin"] == "https://good.test"
    assert _has(response, "access-control-allow-credentials")
    assert _has(response, "access-control-expose-headers")


def test_a_refused_origin_gets_neither_qualifier():
    """Both were sent regardless, advertising credentials for an origin that
    was not allowed - harmless to a browser, misleading in a capture."""
    with TestClient(_qualified_app()) as client:
        response = client.get("/", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}
    assert not _has(response, "access-control-allow-credentials")
    assert not _has(response, "access-control-expose-headers")


def test_a_same_origin_response_carries_no_cors_qualifiers():
    """A request with no Origin is not a CORS request at all."""
    with TestClient(_qualified_app()) as client:
        response = client.get("/")
    assert not _has(response, "access-control-allow-credentials")
    assert not _has(response, "access-control-expose-headers")


def test_a_preflight_for_an_allowed_origin_still_carries_them():
    with TestClient(_qualified_app()) as client:
        response = client.options(
            "/",
            headers={
                "Origin": "https://good.test",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert _has(response, "access-control-allow-credentials")
    assert _has(response, "access-control-expose-headers")


def test_a_wildcard_origin_still_exposes_its_headers():
    """The gate is on an origin being granted, not on it being specific."""
    app = Veloce(openapi_url=None)
    app.add_middleware(CORSMiddleware(allow_origins=["*"], expose_headers=["X-Total"]))

    @app.get("/")
    async def index():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/", headers={"Origin": "https://any.test"})
    assert response.headers["access-control-allow-origin"] == "*"
    assert _has(response, "access-control-expose-headers")


# ── The exposure list is merged, not clobbered ───────────────────────


class _Contributor(Middleware):
    """A middleware that adds one header to the exposure list."""

    name = "contributor"

    def __init__(self, key: str = "Access-Control-Expose-Headers") -> None:
        super().__init__()
        self.key = key

    async def process_response(self, request, response):
        merged = HeaderSet(response.headers.get(self.key, ""))
        merged.update(["X-Board-Page"])
        response.headers[self.key] = merged.to_header()
        return response


def _merging_app(key: str) -> Veloce:
    app = Veloce(openapi_url=None)
    # Higher priority runs later in the response phase, so CORS writes last.
    app.add_middleware(
        CORSMiddleware(allow_origins=["https://good.test"], expose_headers=["X-Total"]),
        priority=85,
    )
    app.add_middleware(_Contributor(key), priority=50)

    @app.get("/")
    async def index():
        return {"ok": True}

    return app


def _exposed(response) -> set[str]:
    values = [
        v for k, v in response.headers.items() if k.lower() == "access-control-expose-headers"
    ]
    assert len(values) == 1, values
    return {token.strip() for token in values[0].split(",")}


@pytest.mark.parametrize("key", ["Access-Control-Expose-Headers", "access-control-expose-headers"])
def test_another_middleware_contribution_survives(key):
    """CORS assigned the header outright, discarding what another had merged.

    `Access-Control-Expose-Headers` is a list header several parties contribute
    to - the shape `Response.add_vary` exists for - so whichever middleware ran
    last won and a header a route meant to expose never reached the browser.
    The casing variant matters because `Response.headers` is a plain dict.
    """
    with TestClient(_merging_app(key)) as client:
        response = client.get("/", headers={"Origin": "https://good.test"})
    assert _exposed(response) == {"X-Total", "X-Board-Page"}


def test_the_configured_headers_are_still_guaranteed():
    app = Veloce(openapi_url=None)
    app.add_middleware(
        CORSMiddleware(allow_origins=["https://good.test"], expose_headers=["X-Total"])
    )

    @app.get("/")
    async def index():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/", headers={"Origin": "https://good.test"})
    assert _exposed(response) == {"X-Total"}


def test_a_duplicate_contribution_is_not_repeated():
    app = Veloce(openapi_url=None)
    app.add_middleware(
        CORSMiddleware(allow_origins=["https://good.test"], expose_headers=["X-Board-Page"]),
        priority=85,
    )
    app.add_middleware(_Contributor(), priority=50)

    @app.get("/")
    async def index():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/", headers={"Origin": "https://good.test"})
    assert _exposed(response) == {"X-Board-Page"}


@pytest.mark.parametrize("pattern", [".*", ".+", "^.*$"])
def test_cors_wildcard_regex_with_credentials_rejected(pattern: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        CORSMiddleware(allow_credentials=True, allow_origin_regex=pattern)
    assert "allow_credentials" in str(excinfo.value)


# ── the CORS usage example runs ───────────────────────────────
#
# Moved here from `test_unswept_scope_findings.py`, a module named for the audit
# batch that produced it rather than for the source it covers.


def _usage_block(obj) -> str:
    doc = inspect.getdoc(obj) or ""
    after = doc.split("Usage::", 1)[1]
    lines, started = [], False
    for line in after.splitlines():
        if not line.strip():
            if started:
                lines.append("")
            continue
        if line.startswith((" ", "\t")):
            started = True
            lines.append(line)
        elif started:
            break
    return textwrap.dedent("\n".join(lines)).strip()


def test_the_cors_usage_example_constructs():
    """The defect: it raised `ValueError` at construction."""
    namespace = {"CORSMiddleware": CORSMiddleware, "app": Veloce(openapi_url=None)}
    exec(compile(_usage_block(CORSMiddleware), "<cors usage>", "exec"), namespace)


def test_the_cors_usage_example_declares_allow_headers():
    """Omitting it is the wildcard that made the example raise."""
    assert "allow_headers" in _usage_block(CORSMiddleware)


def test_credentials_with_wildcard_headers_is_still_refused():
    """The guard the example was tripping must stay."""
    with pytest.raises(ValueError, match="allow_credentials"):
        CORSMiddleware(allow_origins=["https://example.com"], allow_credentials=True)


def test_the_example_actually_answers_a_cors_request():
    """Constructing is not enough - it has to do the job it advertises."""
    app = Veloce(openapi_url=None)
    namespace = {"CORSMiddleware": CORSMiddleware, "app": app}
    exec(compile(_usage_block(CORSMiddleware), "<cors usage>", "exec"), namespace)

    @app.get("/x")
    async def x() -> dict:
        return {}

    response = TestClient(app).get("/x", headers={"Origin": "https://example.com"})
    allowed = [v for k, v in response.headers.items() if k.lower() == "access-control-allow-origin"]
    assert allowed == ["https://example.com"]
