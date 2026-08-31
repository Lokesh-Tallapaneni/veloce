"""app.webhooks — OpenAPI 3.1 webhooks section."""

from __future__ import annotations

from pydantic import BaseModel

from tests._openapi import document
from veloce import Request, Veloce
from veloce.testclient import TestClient


class Subscription(BaseModel):
    id: str
    active: bool


def test_no_webhooks_key_when_none_registered():
    app = Veloce()

    with TestClient(app) as client:
        schema = document(client)

    assert "webhooks" not in schema


def test_webhook_appears_in_schema():
    app = Veloce()

    @app.webhooks.post("new-subscription")
    async def new_subscription(body: Subscription):
        pass

    with TestClient(app) as client:
        schema = document(client)

    assert "new-subscription" in schema["webhooks"]
    assert "post" in schema["webhooks"]["new-subscription"]


def test_webhook_request_body_schema():
    app = Veloce()

    @app.webhooks.post("new-subscription")
    async def new_subscription(body: Subscription):
        pass

    with TestClient(app) as client:
        schema = document(client)

    op = schema["webhooks"]["new-subscription"]["post"]
    ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/Subscription"
    assert "Subscription" in schema["components"]["schemas"]


def test_webhook_route_not_dispatchable():
    app = Veloce()

    @app.webhooks.post("evt")
    async def evt(body: Subscription):
        pass

    @app.get("/real")
    async def real(request: Request):
        return {"ok": True}

    with TestClient(app) as client:
        # The webhook path is documentation-only — not a live route.
        assert client.post("/evt").status_code == 404
        assert client.get("/real").json() == {"ok": True}


def test_multiple_webhooks():
    app = Veloce()

    @app.webhooks.post("created")
    async def created(body: Subscription):
        pass

    @app.webhooks.post("deleted")
    async def deleted(body: Subscription):
        pass

    with TestClient(app) as client:
        schema = document(client)

    assert set(schema["webhooks"]) == {"created", "deleted"}
