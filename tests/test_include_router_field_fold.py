"""A merged route's folded `Field` names the source the *merged* path has.

`include_router(prefix="/tenants/{tenant_id}")` gives a child route a path
parameter the child router did not know about when it planned its handler. The
fold that turns a pydantic `Field` into a parameter marker has to see the merged
template, not the child's: a marker that named `query` for a segment that
arrives in the path stops it binding at all, and with a default present binds
the default instead - wrong data rather than an error.

Reusing the child's plan is still right whenever the prefix adds no path
parameter, which is the common case and the reason the reuse exists.
"""

from __future__ import annotations

import warnings
from typing import Annotated

from pydantic import Field

from veloce import APIRouter, Depends, Veloce
from veloce.testclient import TestClient

TENANT_PREFIX = "/tenants/{tenant_id}"

Bounded = Annotated[str, Field(min_length=1)]


def _bounded_router() -> APIRouter:
    router = APIRouter()

    @router.get("/items")
    async def items(tenant_id: Bounded):
        return {"tenant": tenant_id}

    return router


def _client(router: APIRouter, *, prefix: str = TENANT_PREFIX) -> TestClient:
    app = Veloce()
    app.include_router(router, prefix=prefix)
    return TestClient(app)


# ── the merged path parameter binds ─────────────────────────────────


def test_a_field_annotation_binds_a_prefix_path_param():
    response = _client(_bounded_router()).get("/tenants/acme/items")
    assert response.status_code == 200
    assert response.json() == {"tenant": "acme"}


def test_a_plain_annotation_binds_it_too():
    """The control: without the `Field` this always worked."""
    router = APIRouter()

    @router.get("/items")
    async def items(tenant_id: str):
        return {"tenant": tenant_id}

    assert _client(router).get("/tenants/acme/items").json() == {"tenant": "acme"}


def test_a_field_annotation_with_a_default_does_not_fall_back_to_it():
    router = APIRouter()

    @router.get("/items")
    async def items(tenant_id: Bounded = "fallback"):
        return {"tenant": tenant_id}

    assert _client(router).get("/tenants/acme/items").json() == {"tenant": "acme"}


def test_the_folded_constraint_is_still_enforced():
    """The negative: binding from the path must not cost the bound itself."""
    router = APIRouter()

    @router.get("/items")
    async def items(tenant_id: Annotated[str, Field(min_length=3)]):
        return {"tenant": tenant_id}

    response = _client(router).get("/tenants/ab/items")
    assert response.status_code == 422
    # Not "missing": the value must reach validation from the path and be
    # rejected on its length, which is what the fold is for.
    detail = response.json()["detail"][0]
    assert detail["loc"] == ["path", "tenant_id"]
    assert "missing" not in detail["msg"].lower()


def test_one_router_included_under_two_path_param_prefixes_binds_both():
    app = Veloce()
    router = _bounded_router()
    app.include_router(router, prefix="/a/{tenant_id}")
    app.include_router(router, prefix="/b/{tenant_id}")
    client = TestClient(app)
    assert client.get("/a/one/items").json() == {"tenant": "one"}
    assert client.get("/b/two/items").json() == {"tenant": "two"}


def test_a_websocket_route_merged_under_a_path_param_prefix_binds_it():
    app = Veloce()
    router = APIRouter()

    @router.websocket("/feed")
    async def feed(ws, tenant_id: Bounded):
        await ws.accept()
        await ws.send_text(tenant_id)
        await ws.close()

    app.include_router(router, prefix=TENANT_PREFIX)

    with TestClient(app).websocket_connect("/tenants/acme/feed") as ws:
        assert ws.receive_text() == "acme"


# ── the reuse that must survive ─────────────────────────────────────


def test_a_prefix_without_a_path_param_still_reuses_the_plan():
    """The negative for the fix: a parameter-free prefix rebuilds nothing."""
    app = Veloce()
    router = _bounded_router()
    child_match = router.match("GET", "/items")
    assert child_match is not None
    app.include_router(router, prefix="/v1")
    merged_match = app.match("GET", "/v1/items")
    assert merged_match is not None
    assert merged_match.route_info.handler_plan is child_match.route_info.handler_plan


def test_merging_does_not_repeat_the_childs_registration_warnings():
    """The rebuild re-plans a handler that was already planned once, so its
    registration diagnostics must not be emitted a second time.

    `simplefilter("always")` rather than the `recwarn` fixture: the per-location
    `__warningregistry__` dedup hides the repeat, so `recwarn` passes either way.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        router = APIRouter()

        @router.get("/items")
        async def items(tenant_id: Bounded, opts: list = []):  # noqa: B006
            return {"tenant": tenant_id, "opts": opts}

        def mutable() -> int:
            return len([w for w in caught if "mutable list default" in str(w.message)])

        assert mutable() == 1, "the child registration should warn once"

        app = Veloce()
        app.include_router(router, prefix=TENANT_PREFIX)

        assert mutable() == 1, "the merge rebuild must not repeat it"


def test_a_prefix_param_the_handler_does_not_declare_still_reuses_the_plan():
    """The rebuild is for a parameter that moved, not for any param-ful prefix.

    `build_plan` consults `path_params` only to ask whether one parameter name
    is in it, so a prefix naming a segment the handler never declares cannot
    change the plan - and re-planning every such route would make
    `include_router` pay full reflection again for nothing.
    """
    app = Veloce()
    router = APIRouter()

    @router.get("/items")
    async def items(q: Annotated[str, Field(min_length=1)] = "all"):
        return {"q": q}

    child_match = router.match("GET", "/items")
    assert child_match is not None
    app.include_router(router, prefix=TENANT_PREFIX)
    merged_match = app.match("GET", "/tenants/acme/items")
    assert merged_match is not None
    assert merged_match.route_info.handler_plan is child_match.route_info.handler_plan
    assert TestClient(app).get("/tenants/acme/items?q=x").json() == {"q": "x"}


def test_a_field_on_a_dependency_binds_a_prefix_path_param():
    """`build_plan` threads `path_params` into every `Depends` sub-plan, so a
    dependency declaring the prefix's parameter is affected by the merge just
    as a handler parameter is - and the handler's own parameter names do not
    mention it."""
    app = Veloce()
    router = APIRouter()

    async def tenant_of(tenant_id: Annotated[str, Field(min_length=1)]):
        return tenant_id

    @router.get("/items")
    async def items(tenant: str = Depends(tenant_of)):
        return {"tenant": tenant}

    app.include_router(router, prefix=TENANT_PREFIX)
    response = TestClient(app).get("/tenants/acme/items")
    assert response.status_code == 200
    assert response.json() == {"tenant": "acme"}


def test_a_field_two_dependencies_deep_binds_it_too():
    """The union has to be transitive, not one level."""
    app = Veloce()
    router = APIRouter()

    async def innermost(tenant_id: Annotated[str, Field(min_length=1)]):
        return tenant_id

    async def middle(value: str = Depends(innermost)):
        return value

    @router.get("/items")
    async def items(tenant: str = Depends(middle)):
        return {"tenant": tenant}

    app.include_router(router, prefix=TENANT_PREFIX)
    assert TestClient(app).get("/tenants/acme/items").json() == {"tenant": "acme"}


def test_a_prefix_adding_no_parameter_reuses_a_plan_that_already_had_one():
    """The equality arm of the reuse gate, which `isdisjoint` cannot answer.

    The child declares `{item_id}` itself and the prefix adds nothing, so the
    two path-parameter sets are equal and nothing moved - but `item_id` IS in
    the handler's parameter names, so `isdisjoint` alone would re-plan every
    parameterised child router on `include_router`.
    """
    app = Veloce()
    router = APIRouter()

    @router.get("/items/{item_id}")
    async def items(item_id: Annotated[str, Field(min_length=1)]):
        return {"item": item_id}

    child_match = router.match("GET", "/items/7")
    assert child_match is not None
    app.include_router(router, prefix="/v1")
    merged_match = app.match("GET", "/v1/items/7")
    assert merged_match is not None
    assert merged_match.route_info.handler_plan is child_match.route_info.handler_plan
    assert TestClient(app).get("/v1/items/7").json() == {"item": "7"}
