"""The public dispatch and override aliases work, and agree with what they alias.

`Veloce` exposes three compatibility aliases: `dispatch_request` and
`full_dispatch_request` (both onto `_dispatch_request`) and
`dependency_overrides_provider` (onto `dependency_overrides`). A review found
their bodies byte-identical and noted something sharper — **none of the three has
a call site in `src/`, `tests/` or `docs/`**.

The duplication is two lines and not worth a public-API migration to remove. The
absence of any test is the real gap: three supported entry points that nothing
exercised, so a refactor of `_dispatch_request`'s signature or of the override
storage could break them and the suite would stay green.

These tests close that. They assert each alias produces the same result as the
thing it aliases, rather than asserting a fixed value — an alias that stops
agreeing is exactly the failure worth catching.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import make_request
from veloce import Depends, Request, Response, Veloce
from veloce.testclient import TestClient


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/r")
    async def route():
        return {"ok": True}

    @app.get("/params/{item_id}")
    async def with_params(item_id: int):
        return {"item_id": item_id}

    return app


def _request(app: Veloce, path: str = "/r", method: str = "GET") -> Request:
    """A `Request` shaped as the ASGI transport would build one."""
    return make_request(
        method=method,
        path=path,
        query_string="",
        headers=[(b"host", b"testserver")],
        body=b"",
    )


async def _through(app: Veloce, entry: str, path: str = "/r"):
    request = _request(app, path)
    request.app = app
    return await getattr(app, entry)(request)


# ── the two dispatch aliases ─────────────────────────────────────────


@pytest.mark.parametrize("entry", ["dispatch_request", "full_dispatch_request"])
async def test_the_alias_dispatches_a_request(entry):
    response = await _through(_app(), entry)
    assert response.status_code == 200


@pytest.mark.parametrize("entry", ["dispatch_request", "full_dispatch_request"])
async def test_the_alias_returns_the_handler_body(entry):
    response = await _through(_app(), entry)
    assert b'"ok":true' in response.body.replace(b" ", b"")


@pytest.mark.parametrize("entry", ["dispatch_request", "full_dispatch_request"])
async def test_the_alias_binds_path_parameters(entry):
    response = await _through(_app(), entry, "/params/7")
    assert b"7" in response.body


@pytest.mark.parametrize("entry", ["dispatch_request", "full_dispatch_request"])
async def test_the_alias_answers_a_missing_route(entry):
    response = await _through(_app(), entry, "/nope")
    assert response.status_code == 404


async def test_the_two_aliases_agree():
    """They alias the same thing; asserted rather than assumed."""
    app = _app()
    first = await _through(app, "dispatch_request")
    second = await _through(app, "full_dispatch_request")
    assert first.status_code == second.status_code
    assert first.body == second.body


def test_the_aliases_agree_with_the_served_response():
    """And with what the transport actually serves for the same route.

    Synchronous: `TestClient` drives its own loop, which cannot be entered from
    inside a running one.
    """

    app = _app()
    served = TestClient(app).get("/r")
    aliased = asyncio.run(_through(app, "dispatch_request"))
    assert served.status_code == aliased.status_code
    assert served.body == aliased.body


async def test_the_alias_runs_the_before_request_hooks():
    """`full_dispatch_request` promises the full hook chain; check it runs."""
    seen = []
    app = _app()

    @app.before_request
    async def before(request):
        seen.append("before")

    await _through(app, "full_dispatch_request")
    assert seen == ["before"]


async def test_the_alias_runs_the_after_request_hooks():
    seen = []
    app = _app()

    @app.after_request
    async def after(request, response):
        seen.append("after")
        return response

    await _through(app, "full_dispatch_request")
    assert seen == ["after"]


async def test_a_before_request_short_circuit_is_honoured_through_the_alias():

    app = _app()

    @app.before_request
    async def before(request):
        return Response(body=b"denied", status_code=403)

    response = await _through(app, "full_dispatch_request")
    assert response.status_code == 403


# ── the override alias ───────────────────────────────────────────────
#
# Note the shape difference, which is itself a finding: `dependency_overrides`
# is a **property** and `dependency_overrides_provider` is a **method**. So the
# "alias" cannot be used the way the thing it aliases is - `app.dependency_
# overrides[dep] = fake` works and `app.dependency_overrides_provider[dep] =
# fake` is a `TypeError`. Left as-is because changing it would break any caller
# using the documented-by-signature `provider()` form, and pinned here so the
# difference is visible rather than discovered.


def test_the_override_alias_is_a_method_not_a_property():
    """The asymmetry, stated as a test so it cannot be changed silently."""
    app = Veloce(openapi_url=None)
    assert callable(app.dependency_overrides_provider)
    assert isinstance(app.dependency_overrides, dict)


def test_the_override_alias_returns_the_same_mapping():
    app = Veloce(openapi_url=None)
    assert app.dependency_overrides_provider() is app.dependency_overrides


def test_writing_through_one_alias_is_visible_on_the_other():
    def original(): ...

    def replacement(): ...

    app = Veloce(openapi_url=None)
    app.dependency_overrides[original] = replacement
    assert app.dependency_overrides_provider()[original] is replacement


def test_writing_through_the_alias_is_visible_on_the_property():
    def original(): ...

    def replacement(): ...

    app = Veloce(openapi_url=None)
    app.dependency_overrides_provider()[original] = replacement
    assert app.dependency_overrides[original] is replacement


def test_an_override_written_through_the_alias_takes_effect():
    """The alias must reach the resolver, not just a lookalike dict."""

    async def real():
        return "real"

    app = Veloce(openapi_url=None)

    @app.get("/v")
    async def route(value: str = Depends(real)):
        return {"value": value}

    async def fake():
        return "fake"

    app.dependency_overrides_provider()[real] = fake
    assert TestClient(app).get("/v").json() == {"value": "fake"}


def test_clearing_through_the_alias_restores_the_dependency():
    async def real():
        return "real"

    app = Veloce(openapi_url=None)

    @app.get("/v")
    async def route(value: str = Depends(real)):
        return {"value": value}

    async def fake():
        return "fake"

    client = TestClient(app)
    app.dependency_overrides_provider()[real] = fake
    assert client.get("/v").json() == {"value": "fake"}
    app.dependency_overrides_provider().clear()
    assert client.get("/v").json() == {"value": "real"}
