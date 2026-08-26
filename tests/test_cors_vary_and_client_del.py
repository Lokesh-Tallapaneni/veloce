"""`Vary: Origin` on every origin-dependent response, and a quiet `__del__`.

Two independent low-severity findings.

**CORS omitted `Vary: Origin` when it refused the origin.** The header was added
only where an `Access-Control-Allow-Origin` had been granted:

    https://good.com     ACAO='https://good.com'  Vary='Origin'
    https://evil.com     ACAO=None                Vary=None
    (no Origin)          ACAO=None                Vary=None

A shared cache stores that third response under an unkeyed entry and can later
serve it to an allowed origin, whose browser then blocks a request that should
have succeeded. It fails *closed* - no `ACAO` ever reaches a refused origin - so
this is caching correctness, not a security hole.

The module's own docstring already stated the right rule: "Whenever the allowed
origin depends on the request origin, the response MUST include `Vary: Origin`".
Dependence is a property of the *configuration*, not of one request's outcome, so
the decision moved to construction: anything other than a bare `*` allow-list
without credentials varies by origin, and says so on every response.

That also makes it cheaper - a precomputed bool per response instead of a string
comparison against `"*"`.

**`TestClient.__del__` masked constructor errors.** When `__init__` raised before
`_owns_loop` was set, `__del__` raised `AttributeError` on top of it and the real
error arrived buried:

    Exception ignored in: <function TestClient.__del__>
    AttributeError: 'TestClient' object has no attribute '_owns_loop'
    TypeError: TestClient.__init__() got an unexpected keyword argument ...
"""

from __future__ import annotations

import gc

import pytest

from veloce import CORSMiddleware, Veloce
from veloce.testclient import TestClient


def _cors_app(**options) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(CORSMiddleware(**options))

    @app.get("/")
    async def index() -> dict:
        return {"ok": True}

    return app


def _header(response, name: str) -> str | None:
    return next((v for k, v in response.headers.items() if k.lower() == name.lower()), None)


# ── the refused and origin-less cases now vary ───────────────────────


def test_a_refused_origin_still_varies_on_origin():
    """The defect: no `Vary`, so a cache could reuse this for an allowed origin."""
    client = TestClient(_cors_app(allow_origins=["https://good.com"]))
    response = client.get("/", headers={"Origin": "https://evil.com"})
    assert _header(response, "Access-Control-Allow-Origin") is None
    assert "origin" in (_header(response, "Vary") or "").lower()


def test_a_request_with_no_origin_still_varies_on_origin():
    client = TestClient(_cors_app(allow_origins=["https://good.com"]))
    response = client.get("/")
    assert _header(response, "Access-Control-Allow-Origin") is None
    assert "origin" in (_header(response, "Vary") or "").lower()


def test_an_allowed_origin_still_varies_on_origin():
    """The case that already worked must keep working."""
    client = TestClient(_cors_app(allow_origins=["https://good.com"]))
    response = client.get("/", headers={"Origin": "https://good.com"})
    assert _header(response, "Access-Control-Allow-Origin") == "https://good.com"
    assert "origin" in (_header(response, "Vary") or "").lower()


def test_every_outcome_varies_for_an_allow_list():
    """All three answers cached under one key was the whole bug."""
    client = TestClient(_cors_app(allow_origins=["https://good.com"]))
    for headers in ({"Origin": "https://good.com"}, {"Origin": "https://evil.com"}, {}):
        response = client.get("/", headers=headers)
        assert "origin" in (_header(response, "Vary") or "").lower(), headers


def test_a_regex_allow_list_varies_on_every_outcome():
    client = TestClient(_cors_app(allow_origin_regex=r"https://.*\.good\.com"))
    for headers in ({"Origin": "https://a.good.com"}, {"Origin": "https://evil.com"}, {}):
        response = client.get("/", headers=headers)
        assert "origin" in (_header(response, "Vary") or "").lower(), headers


def test_a_credentialed_config_varies():
    """Credentials force echoing the exact origin, so the value depends on it.

    `allow_headers` is explicit because wildcard headers with credentials is
    refused outright (Fetch CORS Sec. 3.2.4), which the constructor enforces.
    """
    client = TestClient(
        _cors_app(
            allow_origins=["https://good.com"],
            allow_credentials=True,
            allow_headers=["Content-Type"],
        )
    )
    response = client.get("/", headers={"Origin": "https://evil.com"})
    assert "origin" in (_header(response, "Vary") or "").lower()


def test_wildcard_origins_with_credentials_is_still_refused():
    """The guard my first draft of the test above tripped over."""
    with pytest.raises(ValueError, match="allow_credentials"):
        CORSMiddleware(allow_origins=["*"], allow_credentials=True)


# ── the bare wildcard does not vary, because it does not depend ──────


def test_a_bare_wildcard_does_not_vary():
    """`ACAO: *` is the same for every caller, so `Vary` would only fragment
    caches for nothing. The negative direction of the rule."""
    client = TestClient(_cors_app(allow_origins=["*"]))
    response = client.get("/", headers={"Origin": "https://anywhere.com"})
    assert _header(response, "Access-Control-Allow-Origin") == "*"
    assert "origin" not in (_header(response, "Vary") or "").lower()


def test_a_bare_wildcard_with_no_origin_does_not_vary():
    client = TestClient(_cors_app(allow_origins=["*"]))
    assert "origin" not in (_header(client.get("/"), "Vary") or "").lower()


# ── an existing Vary is merged, not replaced ─────────────────────────


def test_an_existing_vary_survives():
    """`Vary` is a list header other middleware contribute to."""
    from veloce import Response

    app = Veloce(openapi_url=None)
    app.add_middleware(CORSMiddleware(allow_origins=["https://good.com"]))

    @app.get("/v")
    async def varies():
        response = Response(body=b"{}", content_type="application/json")
        response.add_vary("Accept-Encoding")
        return response

    response = TestClient(app).get("/v", headers={"Origin": "https://evil.com"})
    vary = (_header(response, "Vary") or "").lower()
    assert "origin" in vary
    assert "accept-encoding" in vary


def test_vary_is_not_duplicated():
    client = TestClient(_cors_app(allow_origins=["https://good.com"]))
    response = client.get("/", headers={"Origin": "https://good.com"})
    vary = (_header(response, "Vary") or "").lower()
    assert vary.count("origin") == 1


# ── the preflight path keeps its own behaviour ───────────────────────


def test_a_preflight_from_an_allowed_origin_varies():
    client = TestClient(_cors_app(allow_origins=["https://good.com"]))
    response = client.options(
        "/",
        headers={"Origin": "https://good.com", "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 204
    assert "origin" in (_header(response, "Vary") or "").lower()


# ── TestClient.__del__ ───────────────────────────────────────────────


def test_del_on_a_half_built_client_does_not_raise():
    """The defect, deterministically.

    Driven through `__new__` so no `__init__` has run and no attribute exists -
    exactly the state a constructor that raised early leaves behind. Going via a
    failing constructor and `gc.collect()` would depend on when the interpreter
    finalises the object, and the resulting `Exception ignored in` goes to the
    unraisable hook rather than to captured stderr.
    """
    client = TestClient.__new__(TestClient)
    client.__del__()  # must not raise


def test_del_on_a_half_built_async_client_does_not_raise():
    from veloce.testclient import AsyncTestClient

    client = AsyncTestClient.__new__(AsyncTestClient)
    deleter = getattr(AsyncTestClient, "__del__", None)
    if deleter is not None:
        deleter(client)


def test_the_real_constructor_error_still_raises():
    """The negative: quieting `__del__` must not swallow the actual failure."""
    with pytest.raises(TypeError, match="definitely_not_a_kwarg"):
        TestClient(Veloce(openapi_url=None), definitely_not_a_kwarg=True)


def test_a_normal_client_still_closes_its_loop():
    """`__del__` still does its job for a client that constructed properly."""
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index() -> dict:
        return {}

    client = TestClient(app)
    assert client.get("/").status_code == 200
    loop = client._loop
    owns = client._owns_loop
    del client
    gc.collect()
    if owns:
        assert loop.is_closed()


def test_the_async_client_del_is_guarded_too():
    """The sync and async clients must not diverge - the project's parity rule."""
    import inspect

    from veloce.testclient import AsyncTestClient

    for cls in (TestClient, AsyncTestClient):
        deleter = getattr(cls, "__del__", None)
        if deleter is None:
            continue
        source = inspect.getsource(deleter)
        assert "getattr(self" in source, f"{cls.__name__}.__del__ reads an attribute unguarded"
