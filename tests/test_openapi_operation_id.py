"""Duplicate operationId detection and deterministic disambiguation."""

from __future__ import annotations

import logging

from veloce import Veloce


def test_duplicate_operation_ids_are_disambiguated() -> None:
    app = Veloce()

    @app.get("/users/{user_id}", name="lookup")
    async def lookup_one(request, user_id: int):
        return {}

    @app.get("/accounts/{account_id}", name="lookup")
    async def lookup_two(request, account_id: int):
        return {}

    schema = app.openapi()
    first = schema["paths"]["/users/{user_id}"]["get"]["operationId"]
    second = schema["paths"]["/accounts/{account_id}"]["get"]["operationId"]
    assert first != second
    # First occurrence keeps the bare id; the duplicate gets a path-derived tail.
    assert first == "lookup_get"
    assert second == "lookup_get__accounts_{account_id}"


def test_disambiguation_is_deterministic_across_rebuilds() -> None:
    app = Veloce()

    @app.get("/a", name="dup")
    async def a(request):
        return {}

    @app.get("/b", name="dup")
    async def b(request):
        return {}

    first = app.openapi()
    app.openapi_schema = None
    second = app.openapi()
    assert first["paths"]["/b"]["get"]["operationId"] == second["paths"]["/b"]["get"]["operationId"]


def test_disambiguation_warns_once(caplog) -> None:
    app = Veloce()

    @app.get("/a", name="dup")
    async def a(request):
        return {}

    @app.get("/b", name="dup")
    async def b(request):
        return {}

    with caplog.at_level(logging.WARNING, logger="veloce.contrib.openapi"):
        app.openapi()
    warnings = [r for r in caplog.records if "operationId" in r.getMessage()]
    assert len(warnings) == 1
    assert "dup_get" in warnings[0].getMessage()


def test_explicit_operation_id_is_left_untouched() -> None:
    app = Veloce()

    @app.get("/a", name="dup", operation_id="pinned")
    async def a(request):
        return {}

    @app.get("/b", name="dup")
    async def b(request):
        return {}

    schema = app.openapi()
    assert schema["paths"]["/a"]["get"]["operationId"] == "pinned"
    # The auto-generated id on /b is unique against the pinned one, untouched.
    assert schema["paths"]["/b"]["get"]["operationId"] == "dup_get"


def test_disambiguation_can_be_disabled() -> None:
    app = Veloce(disambiguate_operation_ids=False)

    @app.get("/a", name="dup")
    async def a(request):
        return {}

    @app.get("/b", name="dup")
    async def b(request):
        return {}

    schema = app.openapi()
    # Opted out: both keep the colliding id (user has accepted responsibility).
    assert schema["paths"]["/a"]["get"]["operationId"] == "dup_get"
    assert schema["paths"]["/b"]["get"]["operationId"] == "dup_get"


def test_webhook_operation_ids_are_disambiguated() -> None:
    # Webhook operations flow through the same disambiguation pass as routes,
    # so two webhook handlers sharing a name+method get distinct operationIds.
    app = Veloce()

    @app.webhooks.post("/new-event", name="notify")
    async def notify(request):
        return {}

    @app.webhooks.post("/other-event", name="notify")
    async def notify_other(request):
        return {}

    schema = app.openapi()
    first = schema["webhooks"]["new-event"]["post"]["operationId"]
    second = schema["webhooks"]["other-event"]["post"]["operationId"]
    assert first != second
    assert first == "notify_post"
    assert second == "notify_post__other-event"


def test_webhook_and_route_operation_id_collision_is_disambiguated() -> None:
    # A webhook colliding with a normal route's auto operationId is resolved
    # too, since both share the document-wide disambiguation pass.
    app = Veloce()

    @app.post("/notify", name="notify")
    async def route_notify(request):
        return {}

    @app.webhooks.post("/notify", name="notify")
    async def hook_notify(request):
        return {}

    schema = app.openapi()
    route_id = schema["paths"]["/notify"]["post"]["operationId"]
    hook_id = schema["webhooks"]["notify"]["post"]["operationId"]
    assert route_id != hook_id


def test_webhook_disambiguation_respects_opt_out() -> None:
    # With disambiguation disabled, colliding webhook ids are left as-is.
    app = Veloce(disambiguate_operation_ids=False)

    @app.webhooks.post("/a", name="notify")
    async def a(request):
        return {}

    @app.webhooks.post("/b", name="notify")
    async def b(request):
        return {}

    schema = app.openapi()
    assert schema["webhooks"]["a"]["post"]["operationId"] == "notify_post"
    assert schema["webhooks"]["b"]["post"]["operationId"] == "notify_post"
