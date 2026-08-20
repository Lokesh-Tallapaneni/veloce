"""Request-dispatch benchmarks — the framework's hot path.

Each app is built once at import time and warmed with a single request so
the lazy OpenAPI setup, the `before_first_request` latch, and the handler
plan caches are already resolved when the measurement starts. Only
`handle_request` is measured.
"""

from __future__ import annotations

from pydantic import BaseModel

from benchmarks.conftest import make_request, run_async
from veloce import (
    CORSMiddleware,
    Depends,
    Query,
    Request,
    Veloce,
)


class Item(BaseModel):
    name: str
    price: float
    quantity: int = 1
    tags: list[str] = []


class ItemOut(BaseModel):
    name: str
    price: float


def _warm(app: Veloce, request_factory) -> Veloce:
    """Drive one request so first-request setup is out of the measurement."""
    run_async(app.handle_request(request_factory()))
    return app


# ── Plain JSON route ───────────────────────────────────────


def _build_json_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/json")
    async def json_hello() -> dict:
        return {"message": "Hello, World!"}

    return _warm(app, lambda: make_request(path="/json"))


JSON_APP = _build_json_app()


def test_dispatch_json_route(benchmark):
    """The canonical "JSON hello" round trip — dispatch with no extras."""
    response = benchmark(lambda: run_async(JSON_APP.handle_request(make_request(path="/json"))))
    assert response.status_code == 200


# ── Path parameters ────────────────────────────────────────


def _build_path_param_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/users/{user_id:int}/orders/{order_id}")
    async def show(user_id: int, order_id: str) -> dict:
        return {"user_id": user_id, "order_id": order_id}

    return _warm(app, lambda: make_request(path="/users/1/orders/a"))


PATH_PARAM_APP = _build_path_param_app()


def test_dispatch_path_params(benchmark):
    """Match, coerce, and bind two path parameters into the handler."""
    response = benchmark(
        lambda: run_async(PATH_PARAM_APP.handle_request(make_request(path="/users/42/orders/x99")))
    )
    assert response.status_code == 200


# ── Query parameters + validation ──────────────────────────


def _build_query_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/search")
    async def search(
        q: str = Query(...),
        limit: int = Query(10, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict:
        return {"q": q, "limit": limit, "offset": offset}

    return _warm(app, lambda: make_request(path="/search", query_string="q=a"))


QUERY_APP = _build_query_app()


def test_dispatch_query_params(benchmark):
    """Parse a query string and validate three constrained parameters."""
    response = benchmark(
        lambda: run_async(
            QUERY_APP.handle_request(
                make_request(path="/search", query_string="q=veloce&limit=25&offset=50")
            )
        )
    )
    assert response.status_code == 200


# ── JSON body validation + response model ──────────────────


def _build_body_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.post("/items", response_model=ItemOut)
    async def create(item: Item) -> ItemOut:
        return ItemOut(name=item.name, price=item.price)

    return _warm(app, lambda: _body_request())


BODY_PAYLOAD = b'{"name":"Thingamajig","price":19.99,"quantity":3,"tags":["new","sale","featured"]}'


def _body_request() -> Request:
    return make_request(
        method="POST",
        path="/items",
        headers={"content-type": "application/json"},
        body=BODY_PAYLOAD,
    )


BODY_APP = _build_body_app()


def test_dispatch_body_validation(benchmark):
    """Decode JSON, validate into a Pydantic model, filter the response."""
    response = benchmark(lambda: run_async(BODY_APP.handle_request(_body_request())))
    assert response.status_code == 200


# ── Dependency injection ───────────────────────────────────


def _build_di_app() -> Veloce:
    app = Veloce(openapi_url=None)

    async def db() -> str:
        return "connection"

    async def settings() -> dict:
        return {"page_size": 20}

    async def paginator(
        settings_: dict = Depends(settings),
        page: int = Query(1, ge=1),
    ) -> dict:
        return {"offset": (page - 1) * settings_["page_size"], "limit": settings_["page_size"]}

    async def current_user(conn: str = Depends(db)) -> dict:
        return {"id": 1, "conn": conn}

    @app.get("/feed")
    async def feed(
        user: dict = Depends(current_user),
        page: dict = Depends(paginator),
    ) -> dict:
        return {"user": user["id"], **page}

    return _warm(app, lambda: make_request(path="/feed"))


DI_APP = _build_di_app()


def test_dispatch_dependency_chain(benchmark):
    """Resolve a four-node dependency graph, two levels deep."""
    response = benchmark(
        lambda: run_async(DI_APP.handle_request(make_request(path="/feed", query_string="page=3")))
    )
    assert response.status_code == 200


# ── Middleware stack ───────────────────────────────────────


def _build_middleware_app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.secret_key = "benchmark-secret-key"
    app.add_middleware(CORSMiddleware(allow_origins=["*"]))

    @app.middleware("http")
    async def stamp(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Bench"] = "1"
        return response

    @app.before_request
    async def before(request: Request) -> None:
        request.state.started = True

    @app.get("/through")
    async def through() -> dict:
        return {"ok": True}

    return _warm(app, lambda: _middleware_request())


def _middleware_request() -> Request:
    return make_request(path="/through", headers={"origin": "https://example.com"})


MIDDLEWARE_APP = _build_middleware_app()


def test_dispatch_through_middleware(benchmark):
    """Full pipeline: CORS, a user middleware, and a before-request hook."""
    response = benchmark(lambda: run_async(MIDDLEWARE_APP.handle_request(_middleware_request())))
    assert response.status_code == 200


# ── Error paths ────────────────────────────────────────────


def test_dispatch_not_found(benchmark):
    """A 404 goes through the exception handlers — it must stay cheap."""
    response = benchmark(
        lambda: run_async(JSON_APP.handle_request(make_request(path="/does-not-exist")))
    )
    assert response.status_code == 404


def test_dispatch_validation_error(benchmark):
    """A 422 builds the full validation-error document."""
    response = benchmark(
        lambda: run_async(
            QUERY_APP.handle_request(make_request(path="/search", query_string="limit=nope"))
        )
    )
    assert response.status_code == 422
