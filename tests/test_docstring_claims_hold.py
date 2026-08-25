"""Claims the code makes about itself, pinned by measurement.

A docstring that is wrong is worse than none: it is read as a specification and
acted on. These were each measured false, and this file holds the corrected
claims to the code so they cannot drift back.

Two carried a behaviour change with them:

* `handle_http_exception` said its body is "byte-identical to what the request
  cycle emits". It substituted `exc.detail or "Error"` where the request cycle
  does not, so an exception carrying an empty detail rendered two different
  bodies depending on which door produced it. Both now build the payload with one
  shared function.
* `_build_asgi_headers(skip_content_length=...)` had a documented `True` case
  that no caller ever passed, so the branch behind it was unreachable. The
  parameter and the branch are gone.

The rest were prose against working code: `has_request_context`,
`DependencyResolver.reset`, `URLRule`'s attributes, `app.aborter`'s lifetime,
`register_blueprint`'s hook handling, `_drain_spawned_tasks`' ordering, the
`EVENT_LOOP_WATCHDOG` comment describing a different key, the two MCP tuple
shapes, and `http_date`'s claim to cost 3 us on every response.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from veloce import (
    Blueprint,
    HTTPException,
    NotFound,
    Veloce,
    has_app_context,
    has_request_context,
)
from veloce.testclient import TestClient

# ── has_request_context is True during dispatch ──────────────────────


def test_a_handler_is_inside_a_request_context():
    """The defect: the docstring said this only happens in a test context."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {"req": has_request_context(), "app": has_app_context()}

    assert TestClient(app).get("/x").json() == {"req": True, "app": True}


def test_a_before_request_hook_is_inside_one():
    app = Veloce(openapi_url=None)
    seen = {}

    @app.before_request
    async def hook(request):
        seen["req"] = has_request_context()
        return None

    @app.get("/x")
    async def x() -> dict:
        return {}

    TestClient(app).get("/x")
    assert seen["req"] is True


def test_outside_a_request_there_is_none():
    """The other half of the claim, which was always true."""
    assert has_request_context() is False


def test_a_test_request_context_still_binds_one():
    app = Veloce(openapi_url=None)
    with app.test_request_context(path="/probe"):
        assert has_request_context() is True


# ── the dispatcher does not touch the resolver on a trivial route ────


def test_a_trivial_route_allocates_no_resolver():
    """The docstring named a dispatcher call site that does not exist."""
    from veloce.dependency import DependencyResolver

    made: list[int] = []
    resets: list[int] = []
    original_init = DependencyResolver.__init__
    original_reset = DependencyResolver.reset

    def counting_init(self, *args, **kwargs):
        made.append(1)
        return original_init(self, *args, **kwargs)

    def counting_reset(self):
        resets.append(1)
        return original_reset(self)

    DependencyResolver.__init__ = counting_init  # type: ignore[method-assign]
    DependencyResolver.reset = counting_reset  # type: ignore[method-assign]
    try:
        app = Veloce(openapi_url=None)

        @app.get("/trivial")
        async def trivial() -> dict:
            return {}

        client = TestClient(app)
        made.clear()
        resets.clear()
        for _ in range(3):
            client.get("/trivial")
    finally:
        DependencyResolver.__init__ = original_init  # type: ignore[method-assign]
        DependencyResolver.reset = original_reset  # type: ignore[method-assign]

    assert made == []
    assert resets == []


def test_reset_is_only_reachable_through_the_resolve_entry_points():
    """The docstring now names these two; a third would make it stale again."""
    import pathlib
    import re

    source = (pathlib.Path(__file__).resolve().parents[1] / "src/veloce/dependency.py").read_text(
        encoding="utf-8"
    )
    assert len(re.findall(r"self\.reset\(\)", source)) == 2


# ── the two error-payload doors agree ────────────────────────────────


@pytest.mark.parametrize(
    ("label", "exc"),
    [("empty detail", HTTPException(404, "")), ("a described error", NotFound())],
)
def test_both_doors_render_the_same_body(label, exc):
    """The defect: an empty detail became `"Error"` out of band and `""` in band."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        raise type(exc)(*([exc.status_code, exc.detail] if exc.detail == "" else []))

    through_request = TestClient(app).get("/x").text
    loop = asyncio.new_event_loop()
    try:
        out_of_band = loop.run_until_complete(app.handle_http_exception(exc)).body.decode()
    finally:
        loop.close()
    assert through_request == out_of_band


def test_an_empty_detail_is_reported_as_empty():
    """Not substituted: the exception said nothing, so the body says nothing."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        raise HTTPException(404, "")

    assert TestClient(app).get("/x").json() == {"detail": "", "status_code": 404}


def test_a_structured_validation_detail_is_still_a_list():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(limit: int) -> dict:
        return {}

    body = TestClient(app).get("/x?limit=abc").json()
    assert isinstance(body["detail"], list)


def test_there_is_one_payload_builder():
    """The two copies differing by one substitution is what caused the drift."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src/veloce/app"
    for name in ("errors.py", "dispatch.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "http_exception_payload" in source, name
    assert 'exc.detail or "Error"' not in (root / "errors.py").read_text(encoding="utf-8")


# ── the ASGI header builder has no unreachable branch ────────────────


def test_the_header_builder_takes_only_the_headers():
    from veloce.app.asgi import _build_asgi_headers

    assert list(inspect.signature(_build_asgi_headers).parameters) == ["headers"]


def test_a_content_length_is_reported_and_emitted():
    from veloce.app.asgi import _build_asgi_headers

    emitted, has_ct, has_cl = _build_asgi_headers({"Content-Length": "3"})
    assert has_cl is True
    assert (b"content-length", b"3") in emitted


def test_a_response_still_reaches_the_client_whole():
    """End to end through the ASGI path the builder serves."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {"a": 1}

    response = TestClient(app).get("/x")
    assert response.json() == {"a": 1}
    assert response.headers["Content-Type"].startswith("application/json")


# ── URLRule carries exactly what it says ─────────────────────────────


def test_urlrule_exposes_its_three_fields():
    from veloce.app.urls import URLRule

    rule = URLRule("/x", ["GET"], "x")
    assert (rule.rule, rule.methods, rule.endpoint) == ("/x", ["GET"], "x")


@pytest.mark.parametrize("attribute", ["defaults", "host", "subdomain"])
def test_urlrule_has_no_other_attributes(attribute):
    """The docstring promised `defaults`, `host`, "etc." - `__slots__` forbids them."""
    from veloce.app.urls import URLRule

    with pytest.raises(AttributeError):
        getattr(URLRule("/x", ["GET"], "x"), attribute)


def test_urlrule_still_unpacks():
    from veloce.app.urls import URLRule

    rule, methods, endpoint = URLRule("/x", ["GET"], "x")
    assert (rule, methods, endpoint) == ("/x", ["GET"], "x")


# ── app.aborter is one instance per application ──────────────────────


def test_the_aborter_is_the_same_object_each_time():
    """The defect: the docstring said a fresh instance per access."""
    app = Veloce(openapi_url=None)
    assert app.aborter is app.aborter


def test_a_mutation_to_the_aborter_sticks():
    app = Veloce(openapi_url=None)
    app.aborter._mapping[499] = ValueError
    assert 499 in app.aborter._mapping


def test_one_apps_aborter_does_not_reach_another():
    first, second = Veloce(openapi_url=None), Veloce(openapi_url=None)
    first.aborter._mapping[499] = ValueError
    assert 499 not in second.aborter._mapping


# ── blueprint hooks are bucketed, not spliced ────────────────────────


def test_a_blueprint_hook_is_not_in_the_app_level_list():
    """The docstring said "splices ... into the app's own lists" and "we tag"."""
    app = Veloce(openapi_url=None)
    bp = Blueprint("shop", url_prefix="/shop")

    @bp.before_request
    async def hook(request):
        return None

    @bp.get("/a")
    async def a() -> dict:
        return {}

    app.register_blueprint(bp)
    assert app._before_request_hooks == []
    assert len(app._bp_before_hooks["shop"]) == 1


def test_a_blueprint_hook_fires_only_on_its_own_routes():
    """The behaviour the bucketing exists to give."""
    app = Veloce(openapi_url=None)
    fired = []
    bp = Blueprint("shop", url_prefix="/shop")

    @bp.before_request
    async def hook(request):
        fired.append(request.path)
        return None

    @bp.get("/a")
    async def a() -> dict:
        return {}

    @app.get("/outside")
    async def outside() -> dict:
        return {}

    app.register_blueprint(bp)
    client = TestClient(app)
    client.get("/shop/a")
    client.get("/outside")
    assert fired == ["/shop/a"]


# ── the MCP registration tuples are the shape the comment names ──────


def test_the_tool_tuple_has_eleven_fields():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add", tags={"math"})
    async def add(a: int, b: int) -> int:
        return a + b

    assert len(app._mcp_tools[0]) == 11


def test_the_prompt_tuple_has_seven_fields():
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="P")
    async def p() -> str:
        return "x"

    assert len(app._mcp_prompts[0]) == 7


def test_the_comments_name_the_real_field_counts():
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / "src/veloce/app/mcp.py").read_text(
        encoding="utf-8"
    )
    assert "task_support, declared, meta, version" in source
    assert "namespace, scopes, icons, meta" in source


# ── nothing on the response path formats a Date header ───────────────


def test_no_date_header_is_emitted():
    """The comment claimed `http_date(None)` costs 3 us on every response."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert not [k for k in TestClient(app).get("/x").headers if k.lower() == "date"]


def test_http_date_still_formats_the_current_time():
    """The function is real and cached; only the claim about its caller was wrong."""
    from veloce.http.dates import http_date

    assert http_date(None).endswith("GMT")
    assert http_date(None) == http_date(None)
