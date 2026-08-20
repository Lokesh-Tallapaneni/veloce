"""Router benchmarks — radix matching, typed converters, reverse lookup.

`Router.match` runs on every single request before any handler code, so
its cost is a floor on the framework's throughput. The routers below are
built once at import time; only the match call is measured.
"""

from __future__ import annotations

import pytest

from veloce import Router

# ── Fixtures built once, outside the measured callables ────


async def _handler(**_kwargs) -> dict[str, str]:
    return {"ok": "yes"}


def _build_router() -> Router:
    """A router with the shape a real API grows into.

    Static routes, single- and multi-parameter templates, typed
    converters, and a `path` catch-all — enough breadth that the static
    map, the radix walk, and the converter coercion each get exercised.
    """
    router = Router()
    for path in (
        "/",
        "/health",
        "/metrics",
        "/api/v1/users",
        "/api/v1/users/me",
        "/api/v1/users/me/settings",
        "/api/v1/orders",
        "/api/v1/orders/pending",
        "/api/v1/products",
        "/api/v1/products/featured",
    ):
        router.add_route(path, _handler, methods=["GET"], name=f"static{path}")
    router.add_route("/api/v1/users/{user_id}", _handler, methods=["GET"], name="user_detail")
    router.add_route(
        "/api/v1/users/{user_id}/orders/{order_id}",
        _handler,
        methods=["GET"],
        name="user_order",
    )
    router.add_route("/api/v1/items/{item_id:int}", _handler, methods=["GET"], name="item_detail")
    router.add_route("/api/v1/keys/{key:uuid}", _handler, methods=["GET"], name="key_detail")
    router.add_route("/files/{file_path:path}", _handler, methods=["GET"], name="file_detail")
    return router


ROUTER = _build_router()

# A deep static path — the radix walk cost grows with depth, the static
# map lookup does not, so both are worth tracking separately.
DEEP_ROUTER = Router()
DEEP_ROUTER.add_route(
    "/a/{b}/c/{d}/e/{f}/g/{h}/i/{j}",
    _handler,
    methods=["GET"],
    name="deep",
)


# ── Matching ───────────────────────────────────────────────


def test_match_static_route(benchmark):
    """Literal path — resolves through the O(1) static map."""
    match = benchmark(ROUTER.match, "GET", "/api/v1/users/me/settings")
    assert match is not None


def test_match_single_path_param(benchmark):
    """One untyped `{param}` — one radix walk plus one capture."""
    match = benchmark(ROUTER.match, "GET", "/api/v1/users/42")
    assert match is not None
    assert match.path_params == {"user_id": "42"}


def test_match_two_path_params(benchmark):
    """Two captures across a deeper template."""
    match = benchmark(ROUTER.match, "GET", "/api/v1/users/42/orders/1337")
    assert match is not None


def test_match_int_converter(benchmark):
    """Typed `int` converter — matching plus coercion."""
    match = benchmark(ROUTER.match, "GET", "/api/v1/items/99")
    assert match is not None
    assert match.path_params == {"item_id": 99}


def test_match_uuid_converter(benchmark):
    """`uuid` converter — the most expensive built-in coercion."""
    match = benchmark(ROUTER.match, "GET", "/api/v1/keys/6ba7b810-9dad-11d1-80b4-00c04fd430c8")
    assert match is not None


def test_match_path_converter(benchmark):
    """`path` catch-all — greedy tail capture."""
    match = benchmark(ROUTER.match, "GET", "/files/docs/guides/getting-started.md")
    assert match is not None


def test_match_deep_parametrized(benchmark):
    """Ten segments, five captures — the radix walk's worst realistic case."""
    match = benchmark(DEEP_ROUTER.match, "GET", "/a/1/c/2/e/3/g/4/i/5")
    assert match is not None


def test_match_miss(benchmark):
    """A 404 still walks the tree — the miss path must stay cheap."""
    match = benchmark(ROUTER.match, "GET", "/api/v1/nothing/here/at/all")
    assert match is None


# ── Reverse lookup ─────────────────────────────────────────


def test_url_for_static(benchmark):
    """`url_for` on a literal route."""
    assert benchmark(ROUTER.url_for, "static/health") == "/health"


def test_url_for_with_params(benchmark):
    """`url_for` substituting and validating two parameters."""
    url = benchmark(lambda: ROUTER.url_for("user_order", user_id=42, order_id=1337))
    assert url == "/api/v1/users/42/orders/1337"


# ── Registration ───────────────────────────────────────────


@pytest.mark.parametrize("route_count", [50])
def test_route_registration(benchmark, route_count: int):
    """Startup cost — building a router of `route_count` routes.

    Registration is where the handler plans get compiled, so it lands on
    every cold start and every reload in development.
    """

    def build() -> Router:
        router = Router()
        for i in range(route_count):
            router.add_route(
                f"/resource{i}/{{resource_id:int}}",
                _handler,
                methods=["GET"],
                name=f"resource_{i}",
            )
        return router

    router = benchmark(build)
    assert router.match("GET", "/resource7/3") is not None
