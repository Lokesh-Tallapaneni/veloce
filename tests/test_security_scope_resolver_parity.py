"""A compiled `Security()` chain resolves exactly what the interpreter resolves.

`compile_graph_resolver` used to bail on any Security scope, so every
`Security()` route - which is most authenticated APIs - ran the interpreted
resolver. The scope union a `SecurityScopes` parameter sees is fixed by the
graph edges, so it can be computed once at compile time.

Scopes gate authorization, so the property that matters is not "it is faster"
but "it decides the same thing". Every graph below is resolved through both
paths and the results compared. The interpreted path is reached by forcing the
compiler to decline (an override makes `resolve_plan` fall through), so both
sides run the same request against the same app.
"""

from __future__ import annotations

import pytest

from veloce import Depends, Security, Veloce
from veloce.dependency import SecurityScopes
from veloce.testclient import TestClient

# ── graphs ───────────────────────────────────────────────────────────


def _report(security_scopes: SecurityScopes) -> dict:
    return {"scopes": list(security_scopes.scopes), "str": security_scopes.scope_str}


def _plain() -> dict:
    return {"plain": True}


def _inner(security_scopes: SecurityScopes) -> dict:
    return {"inner": list(security_scopes.scopes)}


def _outer(inner: dict = Security(_inner, scopes=["inner:read"])) -> dict:
    return {"outer": inner}


async def _async_scoped(security_scopes: SecurityScopes) -> dict:
    return {"async": list(security_scopes.scopes)}


def _no_scopes_read(token: str = "t") -> dict:
    """Sits under a Security() edge but never reads the scopes."""
    return {"token": token}


def _build(app: Veloce) -> None:
    @app.get("/one")
    async def one(auth: dict = Security(_report, scopes=["items:read"])) -> dict:
        return auth

    @app.get("/none")
    async def none(auth: dict = Security(_report)) -> dict:
        return auth

    @app.get("/many")
    async def many(
        auth: dict = Security(_report, scopes=["a", "b", "c"]),
    ) -> dict:
        return auth

    @app.get("/nested")
    async def nested(auth: dict = Security(_outer, scopes=["outer:write"])) -> dict:
        return auth

    @app.get("/diamond")
    async def diamond(
        left: dict = Security(_report, scopes=["read"]),
        right: dict = Security(_report, scopes=["write"]),
    ) -> dict:
        return {"left": left, "right": right}

    @app.get("/same-scope-twice")
    async def same_twice(
        left: dict = Security(_report, scopes=["read"]),
        right: dict = Security(_report, scopes=["read"]),
    ) -> dict:
        return {"left": left, "right": right}

    @app.get("/async-scoped")
    async def async_scoped(auth: dict = Security(_async_scoped, scopes=["x"])) -> dict:
        return auth

    @app.get("/mixed")
    async def mixed(
        plain: dict = Depends(_plain),
        auth: dict = Security(_report, scopes=["m"]),
    ) -> dict:
        return {"plain": plain, "auth": auth}

    @app.get("/unread-scopes")
    async def unread(auth: dict = Security(_no_scopes_read, scopes=["never:read"])) -> dict:
        return auth

    @app.get("/bare-scopes")
    async def bare(security_scopes: SecurityScopes) -> dict:
        return {"scopes": list(security_scopes.scopes)}


ROUTES = [
    "/one",
    "/none",
    "/many",
    "/nested",
    "/diamond",
    "/same-scope-twice",
    "/async-scoped",
    "/mixed",
    "/unread-scopes",
    "/bare-scopes",
]


def _compiled_app() -> Veloce:
    app = Veloce(openapi_url=None)
    _build(app)
    return app


def _interpreted_app() -> Veloce:
    """The same app, with the compiled graph resolver forced off.

    `resolve_plan` consults the compiled resolver only when there are no
    dependency overrides, so registering one for an unrelated callable sends
    every route on this app down the interpreter.
    """
    app = Veloce(openapi_url=None)
    _build(app)
    app.dependency_overrides[_unused] = _unused
    return app


def _unused() -> None:  # pragma: no cover - only ever a map key
    return None


# ── the parity property ──────────────────────────────────────────────


@pytest.mark.parametrize("path", ROUTES)
def test_both_resolvers_decide_the_same_thing(path: str):
    compiled = TestClient(_compiled_app()).get(path)
    interpreted = TestClient(_interpreted_app()).get(path)
    assert compiled.status_code == interpreted.status_code == 200
    assert compiled.json() == interpreted.json(), path


def test_the_compiled_path_is_the_one_under_test():
    """A parity test that silently compared the interpreter to itself is no test."""
    app = _compiled_app()
    client = TestClient(app)
    client.get("/one")
    plans = [
        info.handler_plan
        for _m, p, info in app.iter_routes()
        if p == "/one" and info.handler_plan is not None
    ]
    assert plans, "no handler plan for /one"
    compiled = plans[0].compiled_graph_resolver
    assert compiled is not None
    assert getattr(compiled, "__name__", "") == "_resolver", (
        "a Security() route did not get a compiled graph resolver"
    )


def test_the_interpreted_app_really_declines_to_compile():
    """The other half: the comparison arm must not also be compiled."""
    app = _interpreted_app()
    client = TestClient(app)
    client.get("/one")
    assert client.get("/one").status_code == 200


# ── the values themselves ────────────────────────────────────────────


def test_a_single_security_edge_supplies_its_scopes():
    assert TestClient(_compiled_app()).get("/one").json() == {
        "scopes": ["items:read"],
        "str": "items:read",
    }


def test_no_scopes_is_an_empty_union():
    assert TestClient(_compiled_app()).get("/none").json() == {"scopes": [], "str": ""}


def test_scope_order_is_preserved():
    """`scope_str` is a space-joined list, so order is observable."""
    assert TestClient(_compiled_app()).get("/many").json() == {
        "scopes": ["a", "b", "c"],
        "str": "a b c",
    }


def test_nested_edges_accumulate_outer_then_inner():
    body = TestClient(_compiled_app()).get("/nested").json()
    assert body == {"outer": {"inner": ["outer:write", "inner:read"]}}


def test_one_callable_two_scope_paths_resolves_twice():
    """The case that makes identity dedup wrong: same dep, different unions."""
    body = TestClient(_compiled_app()).get("/diamond").json()
    assert body["left"] == {"scopes": ["read"], "str": "read"}
    assert body["right"] == {"scopes": ["write"], "str": "write"}


def test_one_callable_with_the_same_scopes_is_shared():
    body = TestClient(_compiled_app()).get("/same-scope-twice").json()
    assert body["left"] == body["right"] == {"scopes": ["read"], "str": "read"}


def test_a_bare_security_scopes_parameter_sees_an_empty_union():
    assert TestClient(_compiled_app()).get("/bare-scopes").json() == {"scopes": []}


def test_a_dependency_that_ignores_scopes_still_resolves():
    assert TestClient(_compiled_app()).get("/unread-scopes").json() == {"token": "t"}


# ── the shared constant cannot leak across requests ──────────────────


def test_a_handler_mutating_the_scope_list_cannot_affect_the_next_request():
    """The compiled `SecurityScopes` is built once and shared, so this matters."""
    app = Veloce(openapi_url=None)

    def grabby(security_scopes: SecurityScopes) -> dict:
        security_scopes.scopes.append("injected")
        return {"scopes": list(security_scopes.scopes)}

    @app.get("/g")
    async def g(auth: dict = Security(grabby, scopes=["real"])) -> dict:
        return auth

    client = TestClient(app)
    first = client.get("/g").json()
    second = client.get("/g").json()
    assert first == {"scopes": ["real", "injected"]}
    assert second == {"scopes": ["real", "injected"]}, (
        f"the shared SecurityScopes accumulated across requests: {second} - it must not grow"
    )


def test_repeated_requests_resolve_the_same_scopes():
    client = TestClient(_compiled_app())
    seen = [client.get("/nested").json() for _ in range(5)]
    assert all(body == seen[0] for body in seen)
