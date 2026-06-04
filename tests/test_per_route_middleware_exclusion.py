"""Per-route middleware exclusion - opting a route out of named middleware.

A route may declare `exclude_middleware=["Name", ...]`; each name is matched
against a middleware's `middleware_name` (its `name` attribute, defaulting to
the class name). The opt-out applies symmetrically to both the request and
response phases. Routes that declare no exclusions must keep running every
registered middleware.
"""

from __future__ import annotations

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
