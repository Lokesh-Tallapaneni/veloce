"""Route flag response_model_exclude_defaults — dump-flag behaviour."""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Veloce
from veloce.testclient import TestClient


class Item(BaseModel):
    name: str
    description: str = "unset"
    tax: float = 0.0


def test_defaults_dropped_when_flag_set():
    app = Veloce(openapi_url=None)

    @app.get("/x", response_model=Item, response_model_exclude_defaults=True)
    async def x():
        return {"name": "widget"}

    with TestClient(app) as client:
        body = client.get("/x").json()

    # Only the explicitly-set non-default field survives.
    assert body == {"name": "widget"}


def test_non_default_values_retained():
    app = Veloce(openapi_url=None)

    @app.get("/x", response_model=Item, response_model_exclude_defaults=True)
    async def x():
        return {"name": "widget", "tax": 7.5}

    with TestClient(app) as client:
        body = client.get("/x").json()

    assert body == {"name": "widget", "tax": 7.5}


def test_flag_off_keeps_defaults():
    app = Veloce(openapi_url=None)

    @app.get("/x", response_model=Item)
    async def x():
        return {"name": "widget"}

    with TestClient(app) as client:
        body = client.get("/x").json()

    assert body == {"name": "widget", "description": "unset", "tax": 0.0}


def test_value_equal_to_default_is_dropped():
    app = Veloce(openapi_url=None)

    @app.get("/x", response_model=Item, response_model_exclude_defaults=True)
    async def x():
        # description explicitly given but equal to its default → dropped.
        return {"name": "widget", "description": "unset"}

    with TestClient(app) as client:
        body = client.get("/x").json()

    assert body == {"name": "widget"}


def test_list_response_model_excludes_defaults():
    app = Veloce(openapi_url=None)

    @app.get(
        "/items",
        response_model=list[Item],
        response_model_exclude_defaults=True,
    )
    async def items():
        return [{"name": "a"}, {"name": "b", "tax": 1.0}]

    with TestClient(app) as client:
        body = client.get("/items").json()

    assert body == [{"name": "a"}, {"name": "b", "tax": 1.0}]
