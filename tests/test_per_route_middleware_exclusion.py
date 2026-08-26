"""Per-route middleware exclusion - opting a route out of named middleware.

A route may declare `exclude_middleware=["Name", ...]`; each name is matched
against a middleware's `middleware_name` (its `name` attribute, defaulting to
the class name). The opt-out applies symmetrically to both the request and
response phases, and is keyed on the route matched at dispatch entry - a
before_request hook that rewrites the path to a route with a different
`exclude_middleware` does not change which middleware run. Routes that declare
no exclusions must keep running every registered middleware.
"""

from __future__ import annotations

import pytest

from veloce import Middleware, Request, TestClient, Veloce


class _Tagger(Middleware):
    """Records its name on the request and stamps a response header."""

    def __init__(self, tag: str, *, name: str | None = None) -> None:
        super().__init__(name=name)
        self.tag = tag

    async def process_request(self, request: Request):
        order = getattr(request.state, "order", None)
        if order is None:
            order = []
            request.state.order = order
        order.append(("req", self.tag))
        return None

    async def process_response(self, request: Request, response):
        order = getattr(request.state, "order", None)
        if order is not None:
            order.append(("resp", self.tag))
        response.headers[f"X-Saw-{self.tag}"] = "1"
        return response


def _app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(_Tagger("A"))
    app.add_middleware(_Tagger("B", name="beta"))

    @app.get("/all")
    async def all_route(request: Request):
        return {"order": getattr(request.state, "order", [])}

    @app.get("/skip-a", exclude_middleware=["_Tagger"])
    async def skip_a(request: Request):
        return {"order": getattr(request.state, "order", [])}

    @app.get("/skip-beta", exclude_middleware=["beta"])
    async def skip_beta(request: Request):
        return {"order": getattr(request.state, "order", [])}

    @app.get("/skip-both", exclude_middleware=["_Tagger", "beta"])
    async def skip_both(request: Request):
        return {"order": getattr(request.state, "order", [])}

    return app


def test_route_with_no_exclusion_runs_all_middleware():
    with TestClient(_app()) as client:
        resp = client.get("/all")
        assert resp.status_code == 200
        assert resp.headers.get("X-Saw-A") == "1"
        assert resp.headers.get("X-Saw-B") == "1"


def test_exclude_by_class_name_skips_that_middleware():
    with TestClient(_app()) as client:
        resp = client.get("/skip-a")
        # `_Tagger` instance with default class name is skipped; the
        # instance named "beta" still runs.
        assert "X-Saw-A" not in resp.headers
        assert resp.headers.get("X-Saw-B") == "1"


def test_exclude_by_instance_name_skips_that_instance():
    with TestClient(_app()) as client:
        resp = client.get("/skip-beta")
        assert resp.headers.get("X-Saw-A") == "1"
        assert "X-Saw-B" not in resp.headers


def test_exclusion_is_symmetric_request_and_response():
    with TestClient(_app()) as client:
        resp = client.get("/skip-both")
        # Neither request nor response phase of either middleware ran.
        body = resp.json()
        assert body["order"] == []
        assert "X-Saw-A" not in resp.headers
        assert "X-Saw-B" not in resp.headers


def test_excluded_middleware_cannot_short_circuit():
    """A middleware that short-circuits in process_request is bypassed."""

    class Blocker(Middleware):
        async def process_request(self, request: Request):
            from veloce import JSONResponse

            return JSONResponse({"blocked": True}, status_code=403)

    app = Veloce(openapi_url=None)
    app.add_middleware(Blocker())

    @app.get("/open", exclude_middleware=["Blocker"])
    async def open_route():
        return {"ok": True}

    @app.get("/closed")
    async def closed_route():
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/closed").status_code == 403
        open_resp = client.get("/open")
        assert open_resp.status_code == 200
        assert open_resp.json() == {"ok": True}


def test_chain_cache_recomputes_when_middleware_added():
    """Adding middleware after a route resolved must invalidate the cache."""
    app = Veloce(openapi_url=None)
    app.add_middleware(_Tagger("A"))

    @app.get("/r", exclude_middleware=["beta"])
    async def r(request: Request):
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/r")
        assert resp.headers.get("X-Saw-A") == "1"

    # Add a second middleware whose name is the excluded one; the cached
    # filtered chain must be rebuilt so "beta" is now actually excluded
    # while "A" keeps running.
    app.add_middleware(_Tagger("B", name="beta"))
    with TestClient(app) as client:
        resp = client.get("/r")
        assert resp.headers.get("X-Saw-A") == "1"
        assert "X-Saw-B" not in resp.headers


def test_middleware_name_defaults_to_class_name():
    mw = _Tagger("X")
    assert mw.middleware_name == "_Tagger"
    named = _Tagger("X", name="custom")
    assert named.middleware_name == "custom"


def test_exclusion_is_symmetric_across_before_request_rewrite():
    # A before_request hook may rewrite request.path, causing _resolve_route to
    # re-match a different route with a different `exclude_middleware`. Per-route
    # exclusion is keyed on the route matched at DISPATCH ENTRY (the same match
    # the request phase used), NOT the post-rewrite final route. So the exact set
    # of middleware that ran process_request must equal the set that ran
    # process_response. Here entry `/enter` excludes nothing while the rewrite
    # target `/final` excludes "beta"; the rewrite must not change which
    # middleware run - both A and B must run on both phases.
    app = Veloce(openapi_url=None)
    app.add_middleware(_Tagger("A"))
    app.add_middleware(_Tagger("B", name="beta"))

    @app.before_request
    def rewrite(request: Request):
        if request.path == "/enter":
            request.path = "/final"
        return None

    @app.get("/enter")
    async def enter(request: Request):
        return {"order": getattr(request.state, "order", [])}

    @app.get("/final", exclude_middleware=["beta"])
    async def final(request: Request):
        return {"order": getattr(request.state, "order", [])}

    with TestClient(app) as client:
        resp = client.get("/enter")
        # The request phase populates `order` (visible in the handler body); the
        # response phase runs after the body is captured, so its effect is read
        # from the stamped `X-Saw-*` headers instead.
        order = resp.json()["order"]
        req_set = {tag for phase, tag in order if phase == "req"}
        resp_set = {tag for tag in ("A", "B") if resp.headers.get(f"X-Saw-{tag}") == "1"}
        # Symmetry: the same middleware ran both phases.
        assert req_set == resp_set
        # Entry route excludes nothing, so the rewrite to /final (which excludes
        # beta) is ignored - both A and B run on both phases.
        assert req_set == {"A", "B"}


def test_exclusion_keyed_on_entry_route_not_rewrite_target():
    # The inverse: entry `/skip-entry` excludes "beta", but a before_request
    # rewrite lands on `/plain` which excludes nothing. Exclusion is keyed on the
    # entry route, so "beta" stays excluded on BOTH phases - it never ran
    # process_request, so it must not run process_response either.
    app = Veloce(openapi_url=None)
    app.add_middleware(_Tagger("A"))
    app.add_middleware(_Tagger("B", name="beta"))

    @app.before_request
    def rewrite(request: Request):
        if request.path == "/skip-entry":
            request.path = "/plain"
        return None

    @app.get("/skip-entry", exclude_middleware=["beta"])
    async def skip_entry(request: Request):
        return {"order": getattr(request.state, "order", [])}

    @app.get("/plain")
    async def plain(request: Request):
        return {"order": getattr(request.state, "order", [])}

    with TestClient(app) as client:
        resp = client.get("/skip-entry")
        order = resp.json()["order"]
        req_set = {tag for phase, tag in order if phase == "req"}
        resp_set = {tag for tag in ("A", "B") if resp.headers.get(f"X-Saw-{tag}") == "1"}
        # Symmetry: the entry route excluded "beta" from the request phase, so
        # it is excluded from the response phase too.
        assert req_set == resp_set == {"A"}
        assert "X-Saw-B" not in resp.headers


# ── Finding: built-in middleware must accept and forward `name=` ─────


def test_proxyfix_accepts_name_and_is_targetable_by_exclusion():
    # `add_middleware(ProxyFix, name="edge")` must instantiate (the override
    # used to raise TypeError), and a route-level `exclude_middleware=["edge"]`
    # must be able to target that instance by its overridden name.
    from veloce.middleware.proxy_fix import ProxyFix

    app = Veloce(openapi_url=None)
    app.add_middleware(ProxyFix, name="edge", x_for=1, x_proto=1)

    @app.get("/with-proxyfix")
    async def with_pf(request: Request):
        # ProxyFix ran: a trusted X-Forwarded-Proto rewrites the scope scheme.
        return {"scheme": request.scope.get("scheme")}

    @app.get("/no-proxyfix", exclude_middleware=["edge"])
    async def no_pf(request: Request):
        return {"scheme": request.scope.get("scheme")}

    with TestClient(app) as client:
        forwarded = {"X-Forwarded-Proto": "https"}
        # The middleware instance carries the overridden name.
        assert app.middlewares[0].middleware_name == "edge"
        # On the unexcluded route ProxyFix rewrites the scheme to https.
        assert client.get("/with-proxyfix", headers=forwarded).json()["scheme"] == "https"
        # On the excluded route ProxyFix is skipped, so the scheme is untouched.
        assert client.get("/no-proxyfix", headers=forwarded).json()["scheme"] == "http"


def _builtin_middleware_cases() -> list[tuple[type, dict]]:
    # Every built-in `Middleware` subclass with its own `__init__`, paired with
    # the minimal required args; `name=` is appended by the test below.
    from veloce.middleware.compression import GZipMiddleware
    from veloce.middleware.conditional import ConditionalGetMiddleware
    from veloce.middleware.cors import CORSMiddleware
    from veloce.middleware.csrf import CSRFMiddleware
    from veloce.middleware.logging import LoggingMiddleware, RequestIDMiddleware
    from veloce.middleware.proxy_fix import ProxyFix
    from veloce.middleware.security import (
        CSPMiddleware,
        HTTPSRedirectMiddleware,
        RateLimitMiddleware,
        SecurityHeadersMiddleware,
        TrustedHostMiddleware,
        WebSocketOriginMiddleware,
    )
    from veloce.middleware.sessions import ServerSessionMiddleware, SessionMiddleware

    return [
        (CORSMiddleware, {}),
        (GZipMiddleware, {}),
        (ConditionalGetMiddleware, {}),
        (CSRFMiddleware, {}),
        (LoggingMiddleware, {}),
        (RequestIDMiddleware, {}),
        (ProxyFix, {}),
        (CSPMiddleware, {"policy": "default-src 'self'"}),
        (HTTPSRedirectMiddleware, {}),
        (RateLimitMiddleware, {}),
        (SecurityHeadersMiddleware, {}),
        (TrustedHostMiddleware, {"allowed_hosts": ["example.com"]}),
        (WebSocketOriginMiddleware, {"allowed_origins": ["https://example.com"]}),
        (SessionMiddleware, {"secret_key": "k"}),
        (ServerSessionMiddleware, {}),
    ]


@pytest.mark.parametrize(("cls", "kwargs"), _builtin_middleware_cases())
def test_every_builtin_middleware_accepts_name_kwarg(cls, kwargs):
    # Each built-in must accept `name=` and surface it via `middleware_name`,
    # and fall back to the class name when no override is given.
    named = cls(name="custom", **kwargs)
    assert named.middleware_name == "custom"
    default = cls(**kwargs)
    assert default.middleware_name == cls.__name__


class _UserMiddlewareNoName(Middleware):
    """A user middleware whose __init__ does NOT accept a `name` keyword."""

    def __init__(self, tag: str) -> None:
        # Deliberately no `super().__init__(name=...)` and no `name` parameter,
        # mirroring a typical user subclass written without knowledge of the
        # framework's exclusion-naming mechanism.
        self.tag = tag

    async def process_response(self, request: Request, response):
        response.headers[f"X-User-{self.tag}"] = "1"
        return response


def test_user_middleware_without_name_kwarg_is_nameable_and_excludable():
    # `add_middleware(MyMW, name=...)` must set the override AFTER construction,
    # so a subclass whose constructor rejects `name` can still be named and
    # targeted by `exclude_middleware`.
    app = Veloce(openapi_url=None)
    app.add_middleware(_UserMiddlewareNoName, name="usermw", tag="X")

    @app.get("/runs")
    async def runs(request: Request):
        return {"ok": True}

    @app.get("/skips", exclude_middleware=["usermw"])
    async def skips(request: Request):
        return {"ok": True}

    with TestClient(app) as client:
        # The post-construction override took effect.
        assert app.middlewares[0].middleware_name == "usermw"
        assert client.get("/runs").headers.get("X-User-X") == "1"
        # The route opts out by the overridden name.
        assert "X-User-X" not in client.get("/skips").headers


def test_blueprint_route_preserves_exclude_middleware():
    # Re-registering a blueprint route onto the app (`_readd_route`) must forward
    # `exclude_middleware`; otherwise a webhook route that opted out of a
    # middleware silently has it run again after `register_blueprint`.
    from veloce import Blueprint

    bp = Blueprint("hooks")

    @bp.get("/webhook", exclude_middleware=["_Tagger"])
    async def webhook(request: Request):
        return {"ok": True}

    app = Veloce(openapi_url=None)
    app.add_middleware(_Tagger("A"))
    app.register_blueprint(bp)

    with TestClient(app) as client:
        resp = client.get("/webhook")
        assert resp.status_code == 200
        # `_Tagger` was excluded on the blueprint route and must stay excluded
        # after the route is spliced onto the app.
        assert "X-Saw-A" not in resp.headers


def test_user_middleware_without_name_defaults_to_class_name():
    # With no `name=` override, the exclusion name falls back to the class name
    # even though the constructor never touched `self.name`.
    app = Veloce(openapi_url=None)
    app.add_middleware(_UserMiddlewareNoName, tag="Y")

    @app.get("/skips", exclude_middleware=["_UserMiddlewareNoName"])
    async def skips(request: Request):
        return {"ok": True}

    with TestClient(app) as client:
        assert app.middlewares[0].middleware_name == "_UserMiddlewareNoName"
        assert "X-User-Y" not in client.get("/skips").headers


# ── The name override on an already-built instance ───────────────────


def test_an_already_built_instance_honours_the_name_override():
    """The class form applied `name=`; the instance form dropped it.

    An exclusion keyed on a name that was never applied silently matches
    nothing, so the route keeps a middleware its author opted out of - a
    failure that looks exactly like a working exclusion.
    """
    app = Veloce(openapi_url=None)
    app.add_middleware(_Tagger("A"), name="tagger")
    assert app.middlewares[0].middleware_name == "tagger"


def test_an_instance_name_makes_the_route_exclusion_take_effect():
    app = Veloce(openapi_url=None)
    app.add_middleware(_Tagger("A"), name="tagger")

    @app.get("/on")
    async def on():
        return {"ok": True}

    @app.get("/off", exclude_middleware=["tagger"])
    async def off():
        return {"ok": True}

    with TestClient(app) as client:
        assert "X-Saw-A" in client.get("/on").headers
        assert "X-Saw-A" not in client.get("/off").headers


def test_an_instance_without_a_name_still_uses_its_class_name():
    app = Veloce(openapi_url=None)
    app.add_middleware(_Tagger("A"))
    assert app.middlewares[0].middleware_name == "_Tagger"


def test_a_constructor_argument_passed_with_an_instance_is_refused():
    """Dropping it silently left the caller believing it had been applied."""
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="already-built middleware instance"):
        app.add_middleware(_Tagger("A"), tag="B")


def test_priority_is_still_accepted_alongside_an_instance():
    """`priority` is a framework ordering concern, not a construction argument."""
    app = Veloce(openapi_url=None)
    app.add_middleware(_Tagger("low"), name="low", priority=1)
    app.add_middleware(_Tagger("high"), name="high", priority=5)
    assert [m.middleware_name for m in app.middlewares] == ["high", "low"]
