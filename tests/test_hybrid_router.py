"""Tests for the hybrid router: radix fast path with a regex fallback.

The radix tree is always consulted first and always wins; the compiled-regex
fallback is reached only on a tree miss and only when regex routes exist.
These tests cover the route shapes the tree cannot express (param sharing a
segment with static text, multiple params per segment, raw regex converters,
greedy `:path` with a suffix) and the surfaces that must keep seeing regex
routes (405/OPTIONS, url_for, include_router, OpenAPI).
"""

from __future__ import annotations

from veloce import Veloce
from veloce.contrib.openapi import get_openapi_schema
from veloce.routing.converters import build_route_regex, is_regex_path
from veloce.routing.router import Router

# ── Classification at registration ───────────────────────────────


class TestClassification:
    def test_plain_tree_paths_are_not_regex(self):
        assert is_regex_path("/users/{id}") is False
        assert is_regex_path("/users/{id:int}") is False
        assert is_regex_path("/users/{id:uuid}/posts") is False
        assert is_regex_path("/files/{p:path}") is False
        assert is_regex_path("/colors/{c:any(red,blue)}") is False
        assert is_regex_path("/static") is False

    def test_param_sharing_segment_is_regex(self):
        assert is_regex_path("/v{version:int}/api") is True
        assert is_regex_path("/v{n}/x") is True

    def test_multi_brace_segment_is_regex(self):
        assert is_regex_path("/files/{name}.{ext}") is True

    def test_raw_regex_converter_is_regex(self):
        assert is_regex_path("/items/{id:[0-9]+}") is True

    def test_greedy_path_with_suffix_is_regex(self):
        assert is_regex_path("/{p:path}/edit") is True


# ── Regex compilation ────────────────────────────────────────────


class TestBuildRouteRegex:
    def test_bare_param_matches_one_segment(self):
        pat = build_route_regex("/v{n}/x")
        assert pat.match("/v2/x").groupdict() == {"n": "2"}
        assert pat.match("/v2/3/x") is None

    def test_builtin_converters(self):
        assert build_route_regex("/v{n:int}/x").match("/v-5/x").groupdict() == {"n": "-5"}
        assert build_route_regex("/v{n:int}/x").match("/vabc/x") is None
        assert build_route_regex("/p/{f:float}").match("/p/1.5").groupdict() == {"f": "1.5"}
        assert build_route_regex("/p/{f:float}").match("/p/2") is None

    def test_raw_regex_converter_pattern(self):
        pat = build_route_regex("/items/{id:[0-9]+}")
        assert pat.match("/items/42").groupdict() == {"id": "42"}
        assert pat.match("/items/ab") is None

    def test_multi_brace_segment(self):
        pat = build_route_regex("/files/{name}.{ext}")
        assert pat.match("/files/report.pdf").groupdict() == {"name": "report", "ext": "pdf"}

    def test_greedy_path_with_suffix(self):
        pat = build_route_regex("/{p:path}/edit")
        assert pat.match("/a/b/c/edit").groupdict() == {"p": "a/b/c"}

    def test_static_text_is_escaped(self):
        pat = build_route_regex("/a.b/{x}")
        assert pat.match("/a.b/v").groupdict() == {"x": "v"}
        # The dot is escaped, so it does not match an arbitrary character.
        assert pat.match("/aXb/v") is None


# ── Tree precedence and zero-cost guarantee ──────────────────────


class TestTreePrecedence:
    def test_tree_routes_still_match(self):
        r = Router()

        @r.get("/users/{id:int}")
        async def get_user(id: int):
            return id

        m = r.match("GET", "/users/7")
        assert m is not None
        # The radix tree coerces a typed `{id:int}` converter to int.
        assert m.path_params == {"id": 7}

    def test_no_regex_routes_leaves_match_unchanged(self):
        r = Router()

        @r.get("/a/{x}")
        async def h(x):
            return x

        # No regex route registered → the fallback list is empty and never
        # consulted; a miss is still a plain None.
        assert r._regex_routes == []
        assert r.match("GET", "/a/b") is not None
        assert r.match("GET", "/a/b/c") is None
        assert r.match("GET", "/missing") is None

    def test_tree_wins_over_regex_on_overlap(self):
        r = Router()

        @r.get("/items/{id}")
        async def tree_handler(id):
            return ("tree", id)

        @r.get("/items/{id:[0-9]+}")
        async def regex_handler(id):
            return ("regex", id)

        # Both could match "/items/42"; the radix tree is consulted first and
        # wins, so the regex handler is never reached for this path.
        m = r.match("GET", "/items/42")
        assert m is not None
        assert m.route_info.handler is tree_handler


# ── Regex fallback matching ──────────────────────────────────────


class TestRegexFallback:
    def test_param_in_segment_matches(self):
        r = Router()

        @r.get("/v{version:int}/api")
        async def versioned(version):
            return version

        m = r.match("GET", "/v2/api")
        assert m is not None
        assert m.path_params == {"version": "2"}
        assert r.match("GET", "/vX/api") is None

    def test_raw_regex_converter_matches_and_rejects(self):
        r = Router()

        @r.get("/items/{id:[0-9]+}")
        async def get_item(id):
            return id

        m = r.match("GET", "/items/123")
        assert m is not None
        assert m.path_params == {"id": "123"}
        # Non-digits fall through to a miss, not a 422.
        assert r.match("GET", "/items/abc") is None

    def test_greedy_path_with_suffix_matches(self):
        r = Router()

        @r.get("/{p:path}/edit")
        async def edit(p):
            return p

        m = r.match("GET", "/a/b/c/edit")
        assert m is not None
        assert m.path_params == {"p": "a/b/c"}
        assert r.match("GET", "/a/b/c") is None

    def test_multi_brace_segment_matches(self):
        r = Router()

        @r.get("/files/{name}.{ext}")
        async def get_file(name, ext):
            return (name, ext)

        m = r.match("GET", "/files/report.pdf")
        assert m is not None
        assert m.path_params == {"name": "report", "ext": "pdf"}

    def test_registration_order_first_match_wins(self):
        r = Router()

        @r.get("/{a:path}/x")
        async def first(a):
            return ("first", a)

        @r.get("/{b:path}/x")
        async def second(b):
            return ("second", b)

        m = r.match("GET", "/p/q/x")
        assert m is not None
        assert m.route_info.handler is first


# ── Method handling: 405 / OPTIONS / HEAD ────────────────────────


class TestRegexMethods:
    def test_method_mismatch_reports_allowed_methods(self):
        r = Router()

        @r.post("/items/{id:[0-9]+}")
        async def create(id):
            return id

        # Wrong method → no match, but get_allowed_methods still finds the
        # regex route so the app can emit a 405 / OPTIONS Allow header.
        assert r.match("GET", "/items/5") is None
        allowed = r.get_allowed_methods("/items/5")
        assert "POST" in allowed

    def test_head_falls_back_to_get(self):
        r = Router()

        @r.get("/v{n:int}/x")
        async def h(n):
            return n

        m = r.match("HEAD", "/v3/x")
        assert m is not None
        assert m.path_params == {"n": "3"}


# ── url_for reverse resolution ───────────────────────────────────


class TestRegexUrlFor:
    def test_url_for_resolves_named_regex_route(self):
        r = Router()

        @r.get("/items/{id:[0-9]+}", name="item_detail")
        async def get_item(id):
            return id

        assert r.url_for("item_detail", id=42) == "/items/42"

    def test_url_for_multi_brace_segment(self):
        r = Router()

        @r.get("/files/{slug}.{ext}", name="file")
        async def get_file(slug, ext):
            return (slug, ext)

        assert r.url_for("file", slug="report", ext="pdf") == "/files/report.pdf"

    def test_url_for_param_in_segment(self):
        r = Router()

        @r.get("/v{version:int}/api", name="versioned")
        async def versioned(version):
            return version

        assert r.url_for("versioned", version=2) == "/v2/api"


# ── include_router merging ───────────────────────────────────────


class TestRegexInclude:
    def test_include_merges_regex_routes(self):
        main = Router()
        sub = Router(prefix="/api")

        @sub.get("/items/{id:[0-9]+}", name="item")
        async def get_item(id):
            return id

        main.include_router(sub)

        m = main.match("GET", "/api/items/9")
        assert m is not None
        assert m.path_params == {"id": "9"}
        # The merged regex route is reverse-resolvable on the parent.
        assert main.url_for("item", id=9) == "/api/items/9"

    def test_include_with_extra_prefix(self):
        main = Router()
        sub = Router()

        @sub.get("/v{n:int}/x")
        async def h(n):
            return n

        main.include_router(sub, prefix="/mount")
        m = main.match("GET", "/mount/v4/x")
        assert m is not None
        assert m.path_params == {"n": "4"}

    def test_include_applies_router_dependencies(self):
        main = Router(dependencies=["dep_a"])
        sub = Router()

        @sub.get("/items/{id:[0-9]+}")
        async def get_item(id):
            return id

        main.include_router(sub)
        m = main.match("GET", "/items/1")
        assert m is not None
        assert "dep_a" in m.route_info.dependencies


# ── OpenAPI schema participation ─────────────────────────────────


class TestRegexOpenAPI:
    def test_regex_route_appears_in_schema_with_openapi_path(self):
        app = Veloce()

        @app.get("/items/{id:[0-9]+}")
        async def get_item(id):
            return id

        schema = get_openapi_schema(app)
        # The OpenAPI path strips the converter, exposing a clean `{id}`.
        assert "/items/{id}" in schema["paths"]
        assert "get" in schema["paths"]["/items/{id}"]

    def test_multi_brace_route_openapi_path(self):
        app = Veloce()

        @app.get("/files/{name}.{ext}")
        async def get_file(name, ext):
            return (name, ext)

        schema = get_openapi_schema(app)
        assert "/files/{name}.{ext}" in schema["paths"]

    def test_collect_all_routes_includes_regex(self):
        r = Router()

        @r.get("/items/{id:[0-9]+}")
        async def get_item(id):
            return id

        collected = r._collect_all_routes()
        paths = {path for _method, path, _info in collected}
        assert "/items/{id}" in paths


# ── End-to-end through the app + test client ─────────────────────


class TestRegexEndToEnd:
    def test_regex_route_dispatches(self):
        from veloce.testclient import TestClient

        app = Veloce()

        @app.get("/items/{id:[0-9]+}")
        async def get_item(id):
            return {"id": id}

        @app.get("/v{version:int}/ping")
        async def ping(version):
            return {"version": version}

        with TestClient(app) as client:
            resp = client.get("/items/77")
            assert resp.status_code == 200
            assert resp.json() == {"id": "77"}

            assert client.get("/items/abc").status_code == 404

            resp2 = client.get("/v3/ping")
            assert resp2.status_code == 200
            assert resp2.json() == {"version": "3"}
