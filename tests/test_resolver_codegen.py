"""Tests for the param-only compiled resolver (veloce._resolver_codegen)."""

from __future__ import annotations

import inspect
import linecache
import traceback

from veloce import (
    Body,
    Cookie,
    Depends,
    File,
    Form,
    Header,
    Path,
    Query,
    TestClient,
    Veloce,
)
from veloce._handler_plan import build_plan
from veloce._resolver_codegen import compile_param_resolver
from veloce.dependency import DependencyResolver, _coerce_value
from veloce.exceptions import RequestValidationError
from veloce.http.request import Request


def _compile(handler):
    return compile_param_resolver(build_plan(handler), _coerce_value, RequestValidationError)


def _q_errors(resp):
    # The validation-error detail entries that concern the `q` parameter.
    return [e for e in resp.json()["detail"] if "q" in (e.get("loc") or [])]


def test_422_payload_parity_compiled_vs_interpreter():
    # Same missing/invalid `q` semantics on a compiled handler (params only)
    # and an interpreter handler (a Depends forces the fallback). The 422
    # bodies for `q` must be identical so error shapes do not drift between
    # the two code paths on the same app.
    def _dep():
        return 1

    app = Veloce(openapi_url=None)

    @app.get("/compiled")
    async def compiled(q: int):
        return {"q": q}

    @app.get("/interp")
    async def interp(q: int, _d: int = Depends(_dep)):
        return {"q": q}

    client = TestClient(app)
    # Both paths must actually be what we think they are.
    assert _compile(compiled) is not None  # compiled path
    assert _compile(interp) is None  # falls back to interpreter

    # Missing required.
    assert _q_errors(client.get("/compiled")) == _q_errors(client.get("/interp"))
    # Invalid coercion.
    assert _q_errors(client.get("/compiled?q=x")) == _q_errors(client.get("/interp?q=x"))


def test_compiles_request_only_handler():
    async def h(request):
        return None

    assert _compile(h) is not None


def test_compiles_scalar_path_and_query_handler():
    async def h(x: int, y: str = "d"):
        return None

    assert _compile(h) is not None


def test_does_not_compile_dependency_handler():
    def dep():
        return 1

    async def h(x: int, d: int = Depends(dep)):
        return None

    assert _compile(h) is None


def test_compiles_sync_marker_handler():
    # Query/Header/Cookie/Path markers are synchronous sources, so the
    # compiler now emits straight-line code for them.
    async def h(q: int = Query(gt=0)):
        return None

    assert _compile(h) is not None


def test_does_not_compile_unmarked_list_param():
    # An un-marked list param is a K_QUERY_LIST slot, still interpreter-only.
    async def h(tags: list[str]):
        return None

    assert _compile(h) is None


def test_does_not_compile_body_marker():
    async def h(p: str = Body()):
        return None

    # Body reads `await request.json()`, unreachable from the sync resolver.
    assert _compile(h) is None


def test_does_not_compile_form_marker():
    async def h(p: str = Form()):
        return None

    assert _compile(h) is None


def test_does_not_compile_file_marker():
    async def h(p: bytes = File()):
        return None

    assert _compile(h) is None


def test_query_marker_present_default_optional_and_constraint():
    app = Veloce(openapi_url=None)

    @app.get("/q")
    async def q_route(
        q: int = Query(gt=0),
        page: int = Query(default=1, ge=1),
        opt: str | None = Query(default=None),
    ):
        return {"q": q, "page": page, "opt": opt}

    assert _compile(q_route) is not None
    client = TestClient(app)
    # Present value, default fallback, optional -> None.
    assert client.get("/q?q=5").json() == {"q": 5, "page": 1, "opt": None}
    assert client.get("/q?q=5&page=3&opt=hi").json() == {"q": 5, "page": 3, "opt": "hi"}
    # validate() constraint failure (gt=0).
    r = client.get("/q?q=0")
    assert r.status_code == 422
    assert any("q" in (e.get("loc") or []) for e in r.json()["detail"])
    # Missing required.
    assert client.get("/q").status_code == 422


def test_header_marker_present_default_and_missing():
    app = Veloce(openapi_url=None)

    @app.get("/h")
    async def h_route(
        token: str = Header(alias="x-token"),
        ua: str = Header(default="none", alias="x-ua"),
    ):
        return {"token": token, "ua": ua}

    assert _compile(h_route) is not None
    client = TestClient(app)
    assert client.get("/h", headers={"x-token": "abc"}).json() == {"token": "abc", "ua": "none"}
    assert client.get("/h", headers={"x-token": "abc", "x-ua": "veloce"}).json() == {
        "token": "abc",
        "ua": "veloce",
    }
    # Missing required header -> 422.
    assert client.get("/h").status_code == 422


def test_cookie_marker_present_and_optional():
    app = Veloce(openapi_url=None)

    @app.get("/c")
    async def c_route(sid: str | None = Cookie(default=None)):
        return {"sid": sid}

    assert _compile(c_route) is not None
    client = TestClient(app)
    assert client.get("/c").json() == {"sid": None}
    assert client.get("/c", headers={"cookie": "sid=xyz"}).json() == {"sid": "xyz"}


def test_path_marker_scalar():
    app = Veloce(openapi_url=None)

    @app.get("/p/{item_id}")
    async def p_route(item_id: int = Path(gt=0)):
        return {"item_id": item_id}

    assert _compile(p_route) is not None
    client = TestClient(app)
    assert client.get("/p/7").json() == {"item_id": 7}
    # Constraint failure on the path value.
    assert client.get("/p/0").status_code == 422


def test_list_typed_query_marker():
    app = Veloce(openapi_url=None)

    @app.get("/tags")
    async def tags_route(tags: list[str] = Query(default=[])):
        return {"tags": tags}

    assert _compile(tags_route) is not None
    client = TestClient(app)
    assert client.get("/tags?tags=a&tags=b").json() == {"tags": ["a", "b"]}
    # Empty -> default.
    assert client.get("/tags").json() == {"tags": []}


def test_list_typed_query_marker_int_coercion():
    app = Veloce(openapi_url=None)

    @app.get("/nums")
    async def nums_route(nums: list[int] = Query(default=[])):
        return {"nums": nums}

    assert _compile(nums_route) is not None
    client = TestClient(app)
    assert client.get("/nums?nums=1&nums=2&nums=3").json() == {"nums": [1, 2, 3]}


def test_marker_parity_compiled_vs_interpreter():
    # The compiled and interpreter paths must produce identical 422 bodies for
    # a constraint failure on a Query() marker. A Depends forces the fallback.
    def _dep():
        return 1

    app = Veloce(openapi_url=None)

    @app.get("/cm")
    async def compiled(q: int = Query(gt=0)):
        return {"q": q}

    @app.get("/im")
    async def interp(q: int = Query(gt=0), _d: int = Depends(_dep)):
        return {"q": q}

    assert _compile(compiled) is not None
    assert _compile(interp) is None
    client = TestClient(app)

    def _detail(resp):
        return [e for e in resp.json()["detail"] if "q" in (e.get("loc") or [])]

    assert _detail(client.get("/cm?q=0")) == _detail(client.get("/im?q=0"))
    assert _detail(client.get("/cm")) == _detail(client.get("/im"))


def test_coercion_and_defaults_end_to_end():
    app = Veloce(openapi_url=None)

    @app.get("/m/{x:int}/{y:int}")
    async def multi(x: int, y: int, a: str = "a", b: int = 0):
        return {"x": x, "y": y, "a": a, "b": b}

    client = TestClient(app)
    assert client.get("/m/3/4?a=hi&b=7").json() == {"x": 3, "y": 4, "a": "hi", "b": 7}
    # Defaults apply when query params are absent.
    assert client.get("/m/1/2").json() == {"x": 1, "y": 2, "a": "a", "b": 0}


def test_optional_param_resolves_to_none():
    app = Veloce(openapi_url=None)

    @app.get("/opt")
    async def opt(q: int | None = None):
        return {"q": q}

    assert TestClient(app).get("/opt").json() == {"q": None}


def test_missing_required_query_is_422():
    app = Veloce(openapi_url=None)

    @app.get("/need")
    async def need(q: int):
        return {"q": q}

    assert TestClient(app).get("/need").status_code == 422


def test_invalid_query_coercion_is_422():
    app = Veloce(openapi_url=None)

    @app.get("/c")
    async def c(n: int = 0):
        return {"n": n}

    assert TestClient(app).get("/c?n=notanint").status_code == 422


def test_path_value_takes_precedence_over_query():
    app = Veloce(openapi_url=None)

    @app.get("/p/{name}")
    async def p(name: str):
        return {"name": name}

    # The path binding wins over a same-named query param, matching the
    # interpreter's path-or-query resolution order.
    assert TestClient(app).get("/p/frompath?name=fromquery").json() == {"name": "frompath"}


async def test_compiled_resolver_is_cached_on_plan():
    async def h(x: int):
        return None

    plan = build_plan(h)
    assert plan.compiled_resolver is None  # not yet attempted

    resolver = DependencyResolver()
    req = Request(method="GET", path="/", query_string="x=5", headers=[], body=b"")

    kwargs = await resolver.resolve_plan(plan, req, {})
    assert kwargs == {"x": 5}
    # After first use the compiled function is cached on the plan.
    assert callable(plan.compiled_resolver)


async def test_reused_resolver_clears_state_before_compiled_path():
    # DependencyResolver is public and may be reused across resolves. A prior
    # resolve that registered a yield-style teardown must NOT leak into a later
    # compiled param-only resolve — the compiled fast path must still reset().

    def yielder():
        yield "x"  # yield dep → registers a teardown on the resolver

    async def with_dep(d: str = Depends(yielder)):
        return d

    async def params_only(q: int):
        return q

    resolver = DependencyResolver()
    req = Request(method="GET", path="/", query_string="q=5", headers=[], body=b"")

    async def run():
        await resolver.resolve_plan(build_plan(with_dep), req, {})
        assert resolver._teardowns, "yield dep should have registered a teardown"
        kwargs = await resolver.resolve_plan(build_plan(params_only), req, {})
        assert kwargs == {"q": 5}
        # The compiled fast path must have reset() first, clearing A's teardown.
        assert resolver._teardowns == [], "stale teardown leaked into the compiled path"

    await run()


# ── generated code is debuggable ─────────────────────────────────────
#
# The resolver is `exec`-compiled, and it was compiled under the fixed filename
# "<veloce-resolver>" with its source thrown away. Nothing could map that name
# back to a line, so a failure inside generated code produced a frame with no
# source:
#
#     File "<veloce-resolver>", line 4, in _resolver
#     TypeError: ...
#
# - no code shown, and every resolver in the process claimed the same name, so
# even reconstructing one by hand told you nothing about which route it was.
#
# Registering the source in `linecache` under a per-resolver filename is what
# the interpreter already consults when formatting a traceback, so the frame
# renders its line like any other. It costs one dict entry per compiled
# resolver, written at registration time, and nothing per request.


def _resolver_of(handler):

    plan = build_plan(handler)
    return compile_param_resolver(plan, _coerce_value, RequestValidationError)


async def _one(q: int = 0):
    return q


async def _two(page: int = 1):
    return page


def test_the_generated_frame_shows_its_source():
    """The defect: a traceback through generated code showed no line."""
    resolver = _resolver_of(_one)
    filename = resolver.__code__.co_filename
    frame = traceback.StackSummary.from_list([(filename, 2, "_resolver", None)]).format()
    rendered = "".join(frame)
    assert filename in rendered
    # `from_list` with `None` text makes the formatter consult linecache, which
    # is exactly the path the interpreter takes when rendering a real traceback.
    assert linecache.getline(filename, 2).strip() != ""


def test_each_resolver_gets_its_own_filename():
    """One shared name meant one resolver's source described them all."""
    first = _resolver_of(_one)
    second = _resolver_of(_two)
    assert first.__code__.co_filename != second.__code__.co_filename


def test_the_filename_names_the_handler():
    """So a frame identifies which route generated it."""
    assert "_one" in _resolver_of(_one).__code__.co_filename


def test_the_registered_source_is_the_resolver_that_ran():
    """A per-resolver entry must hold that resolver's own code."""
    resolver = _resolver_of(_one)
    source = "".join(linecache.getlines(resolver.__code__.co_filename))
    assert "def _resolver(" in source
    assert "'q'" in source or '"q"' in source


def test_checkcache_does_not_evict_the_entry():
    """`linecache.checkcache` runs on every traceback; a stat-based entry for a
    filename with no file on disk would be dropped before it was ever read."""
    resolver = _resolver_of(_one)
    filename = resolver.__code__.co_filename
    assert linecache.getline(filename, 1) != ""
    linecache.checkcache()
    assert linecache.getline(filename, 1) != ""


def test_inspect_can_read_the_generated_source():
    resolver = _resolver_of(_one)
    assert "def _resolver(" in inspect.getsource(resolver)


def test_a_graph_resolver_is_registered_too():
    """The second generator gets the same treatment.

    Driven through a real request: the graph resolver is built by the resolver
    on first use, not by `build_plan`, so serving the route is what compiles it.
    """

    async def dep(q: int = 0):
        return q

    app = Veloce(openapi_url=None)

    @app.get("/chained")
    async def chained(value: int = Depends(dep)):
        return {"value": value}

    with TestClient(app) as tc:
        assert tc.get("/chained?q=3").json() == {"value": 3}

    plans = [
        info.handler_plan
        for _method, _path, info in app._collect_all_routes(include_hidden=True)
        if info.path_template == "/chained"
    ]
    compiled = [p.compiled_graph_resolver for p in plans if p is not None]
    resolver = next((c for c in compiled if callable(c)), None)
    # Asserted, not skipped. This bare-`return`ed when no compiled resolver was
    # found, so the two assertions below were silently skipped in exactly the
    # case they exist to detect: a compiler that stopped compiling this shape
    # left the module green. A chained `Depends` on a plain scalar is the
    # canonical compiled shape - if it stops compiling, this must fail.
    assert resolver is not None, "the chained-Depends shape no longer compiles a graph resolver"
    assert linecache.getline(resolver.__code__.co_filename, 1) != ""
    assert "graph-resolver" in resolver.__code__.co_filename


def test_registration_still_returns_a_working_resolver():
    """The negative: naming and registering must not disturb the result."""
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def items(q: int = 0):
        return {"q": q}

    with TestClient(app) as tc:
        assert tc.get("/items?q=7").json() == {"q": 7}


def test_the_filename_is_stable_for_the_same_generated_code():
    """Documented in the debugging guide: the digest changes only when the
    generated code does, so a frame is comparable across runs and processes."""
    assert _resolver_of(_one).__code__.co_filename == _resolver_of(_one).__code__.co_filename


def test_recompiling_one_plan_adds_one_cache_entry():
    """A counter-keyed name grew `linecache` without bound - a re-registered
    route or a test suite would accumulate an entry per compile, never freed."""
    before = {k for k in linecache.cache if k.startswith("<veloce-")}
    for _ in range(25):
        _resolver_of(_one)
    after = {k for k in linecache.cache if k.startswith("<veloce-")}
    assert len(after - before) <= 1


def test_a_different_plan_gets_its_own_entry():
    """Bounded must not mean shared: two resolvers keep two sources."""
    first = _resolver_of(_one).__code__.co_filename
    second = _resolver_of(_two).__code__.co_filename
    assert first != second
