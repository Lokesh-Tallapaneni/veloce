"""Tests for the pre-built handler plan (D15)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from veloce import Depends, Header, Query, Request, Veloce
from veloce._handler_plan import (
    K_BG_TASKS,
    K_BODY_MODEL,
    K_DEPENDS,
    K_PARAM_MARKER,
    K_QUERY,
    K_QUERY_LIST,
    K_REQUEST,
    K_UPLOAD_FILE,
    build_plan,
    build_route_dep_plans,
)
from veloce.background import BackgroundTasks
from veloce.http.datastructures import UploadFile


class _Item(BaseModel):
    name: str


# ── Plan-construction tests ──────────────────────────────────────────


def test_plan_recognises_request_by_annotation():
    async def h(request: Request):
        return None

    plan = build_plan(h)
    assert len(plan.slots) == 1
    assert plan.slots[0].kind == K_REQUEST
    assert plan.is_coro is True


def test_plan_recognises_request_by_name():
    async def h(request):  # no annotation
        return None

    plan = build_plan(h)
    assert plan.slots[0].kind == K_REQUEST


def test_plan_recognises_background_tasks():
    async def h(bg: BackgroundTasks):
        return None

    assert build_plan(h).slots[0].kind == K_BG_TASKS


def test_plan_recognises_depends():
    def dep():
        return 1

    async def h(x=Depends(dep)):
        return x

    plan = build_plan(h)
    slot = plan.slots[0]
    assert slot.kind == K_DEPENDS
    assert slot.dep_callable is dep
    assert slot.use_cache is True
    assert slot.sub_plan is not None  # recursively planned
    assert slot.dep_is_coro is False


def test_plan_recognises_param_marker():
    async def h(q: str = Query(default="x", alias="search")):
        return q

    plan = build_plan(h)
    slot = plan.slots[0]
    assert slot.kind == K_PARAM_MARKER
    assert slot.marker_kind == 0  # MK_QUERY
    assert slot.lookup_name == "search"


def test_plan_recognises_header_marker():
    async def h(x_token: str = Header(alias="X-Token")):
        return x_token

    slot = build_plan(h).slots[0]
    assert slot.kind == K_PARAM_MARKER
    assert slot.marker_kind == 2  # MK_HEADER
    assert slot.lookup_name == "X-Token"


def test_plan_recognises_body_model():
    async def h(item: _Item):
        return item

    slot = build_plan(h).slots[0]
    assert slot.kind == K_BODY_MODEL
    assert slot.model is _Item


def test_plan_recognises_upload_file():
    async def h(file: UploadFile):
        return file

    slot = build_plan(h).slots[0]
    assert slot.kind == K_UPLOAD_FILE
    assert slot.is_optional is False


def test_plan_recognises_optional_upload_file():
    async def h(file: UploadFile | None = None):
        return file

    slot = build_plan(h).slots[0]
    assert slot.kind == K_UPLOAD_FILE
    assert slot.is_optional is True


def test_plan_recognises_query_list():
    async def h(tags: list[str] = []):  # noqa: B006
        return tags

    slot = build_plan(h).slots[0]
    assert slot.kind == K_QUERY_LIST
    assert slot.list_inner is str


def test_plan_falls_back_to_query_for_bare_typed_params():
    async def h(name: str):
        return name

    slot = build_plan(h).slots[0]
    assert slot.kind == K_QUERY
    assert slot.target_type is str


def test_plan_records_defaults():
    async def h(page: int = 1):
        return page

    slot = build_plan(h).slots[0]
    assert slot.has_default is True
    assert slot.default == 1


def test_plan_route_dep_plans():
    def auth():
        return "ok"

    def admin():
        return True

    plans = build_route_dep_plans([Depends(auth), Depends(admin)])
    assert len(plans) == 2
    assert all(p.kind == K_DEPENDS for p in plans)
    assert plans[0].dep_callable is auth
    assert plans[1].dep_callable is admin


def test_plan_skips_self_parameter():
    class V:
        async def h(self, request: Request):
            return None

    plan = build_plan(V.h)
    # self is skipped; only request remains.
    assert [s.kind for s in plan.slots] == [K_REQUEST]


def test_plan_tolerant_to_broken_annotations():
    # Forward-ref to a name that won't resolve at get_type_hints time. The
    # plan must still build (treating the annotation as absent), not raise.
    def h(x: "DoesNotExistAnywhere") -> None:  # type: ignore[name-defined]  # noqa: F821, UP037
        return None

    plan = build_plan(h)
    assert len(plan.slots) == 1
    # Without resolved annotation, falls through to K_QUERY with str default.
    assert plan.slots[0].kind == K_QUERY
    assert plan.slots[0].target_type is str


# ── Integration: app.handle_request uses the plan ─────────────────────


@pytest.fixture
def app() -> Veloce:
    return Veloce(debug=True, openapi_url=None)


def _make_request(**kw):
    defaults = dict(method="GET", path="/", query_string="", headers={}, body=b"")
    defaults.update(kw)
    return Request(**defaults)


@pytest.mark.asyncio
async def test_route_info_carries_plan(app: Veloce):
    @app.get("/x")
    async def x(q: str = Query(default="y")):
        return {"q": q}

    routes = app._collect_all_routes()
    assert len(routes) == 1
    _, _, info = routes[0]
    assert info.handler_plan is not None
    assert len(info.handler_plan.slots) == 1
    assert info.handler_plan.slots[0].kind == K_PARAM_MARKER


@pytest.mark.asyncio
async def test_handler_with_depends_still_resolves(app: Veloce):
    def get_user():
        return {"id": 1}

    @app.get("/me")
    async def me(user=Depends(get_user)):
        return user

    resp = await app.handle_request(_make_request(path="/me"))
    assert resp.status_code == 200
    assert b'"id":1' in resp.body


@pytest.mark.asyncio
async def test_dependency_overrides_still_work(app: Veloce):
    def real():
        return "real"

    def fake():
        return "fake"

    @app.get("/echo")
    async def echo(v=Depends(real)):
        return {"v": v}

    app._dependency_overrides[real] = fake
    resp = await app.handle_request(_make_request(path="/echo"))
    assert b'"fake"' in resp.body


@pytest.mark.asyncio
async def test_plan_avoids_inspect_signature_on_hot_path(app: Veloce, monkeypatch):
    # The plan must be consulted; `inspect.signature` should not be called
    # again from inside DependencyResolver during a request.
    @app.get("/fast")
    async def fast(request: Request):
        return {"ok": True}

    # First request triggers any lazy imports — exclude that.
    await app.handle_request(_make_request(path="/fast"))

    import inspect as _inspect

    calls = {"n": 0}
    real = _inspect.signature

    def counting(fn, *a, **kw):
        calls["n"] += 1
        return real(fn, *a, **kw)

    monkeypatch.setattr("inspect.signature", counting)
    await app.handle_request(_make_request(path="/fast"))
    # Zero signature calls expected on the dispatch path.
    assert calls["n"] == 0, "inspect.signature should not run on the hot path"


def test_class_dependency_resolves_init_annotations():
    """A class used as a dependency coerces __init__ params by annotation."""
    app = Veloce(openapi_url=None)

    class Pager:
        def __init__(self, page: int = 1):
            self.page = page
            self.page_type = type(page).__name__

    @app.get("/pager")
    async def pager(p: Pager = Depends(Pager)):
        return {"page_value": p.page, "page_type": p.page_type}

    body = app.test_client().get("/pager?page=5").json()
    assert body == {"page_value": 5, "page_type": "int"}


def test_partial_dependency_resolves_wrapped_annotations():
    """A functools.partial dependency keeps the wrapped function's annotations."""
    import functools

    app = Veloce(openapi_url=None)

    def make_pager(page: int = 1, fixed: str = "x"):
        return {"page": page, "page_type": type(page).__name__}

    pager_dep = functools.partial(make_pager, fixed="bound")

    @app.get("/p")
    async def p(data: dict = Depends(pager_dep)):
        return data

    assert app.test_client().get("/p?page=7").json() == {"page": 7, "page_type": "int"}


def test_response_import_is_module_level_in_dependency():
    """P-3: the `Response` symbol must be bound on the dependency
    module at import time. The previous inline `from veloce.http.response
    import Response` inside `_resolve_slots` paid an import-system
    lookup on every request whose handler injected a Response."""
    import veloce.dependency as dep
    from veloce.http.response import Response

    assert hasattr(dep, "Response")
    assert dep.Response is Response
