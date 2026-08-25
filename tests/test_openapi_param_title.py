"""Query/Path `title` emitted into the OpenAPI parameter schema."""

from __future__ import annotations

from veloce import Path, Query, Veloce
from veloce.testclient import TestClient


def _params(schema: dict, path: str, method: str = "get") -> list[dict]:
    return schema["paths"][path][method].get("parameters", [])


def test_query_title_emitted():
    app = Veloce()

    @app.get("/search")
    async def search(q: str = Query(default="", title="Search Term")):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    params = _params(schema, "/search")
    q = [p for p in params if p["name"] == "q"][0]
    assert q["schema"]["title"] == "Search Term"


def test_path_title_emitted():
    app = Veloce()

    @app.get("/items/{item_id}")
    async def item(item_id: int = Path(title="Item Identifier")):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    params = _params(schema, "/items/{item_id}")
    p = [p for p in params if p["name"] == "item_id"][0]
    assert p["schema"]["title"] == "Item Identifier"


def test_no_title_key_when_unset():
    app = Veloce()

    @app.get("/plain")
    async def plain(q: str = Query(default="")):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    q = [p for p in _params(schema, "/plain") if p["name"] == "q"][0]
    assert "title" not in q["schema"]


def test_title_coexists_with_description():
    app = Veloce()

    @app.get("/both")
    async def both(q: str = Query(default="", title="The Query", description="What to find")):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    q = [p for p in _params(schema, "/both") if p["name"] == "q"][0]
    assert q["schema"]["title"] == "The Query"
    assert q["schema"]["description"] == "What to find"
