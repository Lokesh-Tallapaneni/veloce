"""Query/Path `title` emitted into the OpenAPI parameter schema."""

from __future__ import annotations

from tests._openapi import document, parameters
from veloce import Path, Query, Veloce
from veloce.testclient import TestClient


def test_query_title_emitted():
    app = Veloce()

    @app.get("/search")
    async def search(q: str = Query(default="", title="Search Term")):
        return {}

    with TestClient(app) as client:
        schema = document(client)

    params = parameters(schema, "/search")
    q = [p for p in params if p["name"] == "q"][0]
    assert q["schema"]["title"] == "Search Term"


def test_path_title_emitted():
    app = Veloce()

    @app.get("/items/{item_id}")
    async def item(item_id: int = Path(title="Item Identifier")):
        return {}

    with TestClient(app) as client:
        schema = document(client)

    params = parameters(schema, "/items/{item_id}")
    p = [p for p in params if p["name"] == "item_id"][0]
    assert p["schema"]["title"] == "Item Identifier"


def test_no_title_key_when_unset():
    app = Veloce()

    @app.get("/plain")
    async def plain(q: str = Query(default="")):
        return {}

    with TestClient(app) as client:
        schema = document(client)

    q = [p for p in parameters(schema, "/plain") if p["name"] == "q"][0]
    assert "title" not in q["schema"]


def test_title_coexists_with_description():
    app = Veloce()

    @app.get("/both")
    async def both(q: str = Query(default="", title="The Query", description="What to find")):
        return {}

    with TestClient(app) as client:
        schema = document(client)

    q = [p for p in parameters(schema, "/both") if p["name"] == "q"][0]
    assert q["schema"]["title"] == "The Query"
    assert q["schema"]["description"] == "What to find"
