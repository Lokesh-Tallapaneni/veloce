"""Route `defaults={...}` — the routing-rule defaults (R19)."""

from __future__ import annotations

from veloce import Request, Veloce
from veloce.testclient import TestClient


def test_default_injected_when_segment_absent():
    app = Veloce()

    @app.get("/articles", defaults={"page": 1})
    @app.get("/articles/page/{page:int}")
    async def articles(request: Request, page: int):
        return {"page": page}

    with TestClient(app) as client:
        # No page segment → default 1.
        assert client.get("/articles").json() == {"page": 1}
        # Explicit segment → that value.
        assert client.get("/articles/page/5").json() == {"page": 5}


def test_default_does_not_override_url_param():
    app = Veloce()

    @app.get("/x/{slug:str}", defaults={"slug": "fallback"})
    async def x(request: Request, slug: str):
        return {"slug": slug}

    with TestClient(app) as client:
        # URL-supplied slug wins over the default.
        assert client.get("/x/real").json() == {"slug": "real"}


def test_default_for_non_url_param():
    app = Veloce()

    @app.get("/dash", defaults={"mode": "summary"})
    async def dash(request: Request, mode: str):
        return {"mode": mode}

    with TestClient(app) as client:
        assert client.get("/dash").json() == {"mode": "summary"}


def test_no_defaults_means_empty_dict():
    app = Veloce()

    @app.get("/plain")
    async def plain():
        return {}

    # RouteInfo.defaults defaults to an empty dict.
    for _m, path, info in app._collect_all_routes():
        if path == "/plain":
            assert info.defaults == {}
            break
    else:
        raise AssertionError("route not found")
