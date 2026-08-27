"""PEP 593 `Annotated[T, Depends(...)]` support (D11).

NOTE: this test file deliberately does NOT use
`from __future__ import annotations`. PEP 593 metadata that carries
runtime objects (a `Depends()` instance bound to a local function) needs
eager annotation evaluation — string annotations would force callers to
keep markers at module scope. This is the documented usage
pattern.
"""

from typing import Annotated

import pytest

from tests.conftest import make_request
from veloce import Depends, Header, Query, Request, Security, SecurityScopes, Veloce


def _req(path: str = "/x", query: str = "") -> Request:
    return make_request(method="GET", path=path, query_string=query, headers={}, body=b"")


# ── Annotated + Depends ───────────────────────────────────────────────


async def test_annotated_depends_resolves():
    """`db: Annotated[Session, Depends(get_db)]` works without a default value."""
    app = Veloce(debug=True, openapi_url=None)

    def get_db() -> dict:
        return {"connected": True}

    @app.get("/x")
    async def x(db: Annotated[dict, Depends(get_db)]):
        return db

    resp = await app.handle_request(_req())
    assert resp.status_code == 200
    assert b'"connected":true' in resp.body


async def test_annotated_depends_with_sub_depends():
    """Annotated form composes — sub-dependencies still resolve."""
    app = Veloce(debug=True, openapi_url=None)

    def get_id() -> int:
        return 42

    def get_user(id: Annotated[int, Depends(get_id)]) -> dict:
        return {"id": id, "name": "alice"}

    @app.get("/me")
    async def me(user: Annotated[dict, Depends(get_user)]):
        return user

    resp = await app.handle_request(_req("/me"))
    assert b'"id":42' in resp.body
    assert b'"name":"alice"' in resp.body


# ── Annotated + Security ──────────────────────────────────────────────


async def test_annotated_security_passes_scopes():
    """Annotated form preserves Security.scopes for the scope stack."""

    app = Veloce(debug=True, openapi_url=None)
    captured: dict = {}

    def auth(scopes: SecurityScopes) -> str:
        captured["scopes"] = list(scopes.scopes)
        return "user"

    @app.get("/x")
    async def x(user: Annotated[str, Security(auth, scopes=["admin"])]):
        return {"user": user}

    await app.handle_request(_req())
    assert captured["scopes"] == ["admin"]


# ── Annotated + Query / Header / Path markers ─────────────────────────


async def test_annotated_query_marker():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x(q: Annotated[str, Query(default="default")]):
        return {"q": q}

    resp = await app.handle_request(_req(query="q=hello"))
    assert b'"hello"' in resp.body


async def test_annotated_query_uses_default_when_missing():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x(q: Annotated[str, Query(default="fallback")]):
        return {"q": q}

    resp = await app.handle_request(_req())
    assert b'"fallback"' in resp.body


async def test_annotated_header_marker_with_alias():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x(token: Annotated[str, Header(alias="X-Token", default="")]):
        return {"token": token}

    req = Request(
        method="GET",
        path="/x",
        query_string="",
        headers={"X-Token": "abc"},
        body=b"",
    )
    resp = await app.handle_request(req)
    assert b'"abc"' in resp.body


# ── Default-value form still wins when both are set ───────────────────


async def test_default_value_wins_over_annotated_when_both_present():
    """When both `Annotated[T, Depends(a)]` AND `= Depends(b)` are set,
    the default-value form takes precedence (matches user intent on the
    visible call site)."""
    app = Veloce(debug=True, openapi_url=None)

    def in_annotation() -> str:
        return "annotated"

    def in_default() -> str:
        return "default"

    @app.get("/x")
    async def x(
        v: Annotated[str, Depends(in_annotation)] = Depends(in_default),
    ):
        return {"v": v}

    resp = await app.handle_request(_req())
    assert b'"default"' in resp.body


# ── Mixed with regular annotations ────────────────────────────────────


async def test_annotated_does_not_break_plain_query_params():
    """Non-Depends Annotated metadata is ignored; the inner type is used
    for query coercion."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x(count: Annotated[int, "some unrelated annotation"] = 0):
        return {"count": count, "type": type(count).__name__}

    resp = await app.handle_request(_req(query="count=7"))
    body = resp.body
    assert b'"count":7' in body
    assert b'"type":"int"' in body


# Module-level so the closure default captures real callables. A two-step
# rebind constructs a true `a -> b -> a` cycle (def-time evaluation of
# defaults makes a single-pass mutual definition impossible).
def _cycle_a(x=Depends(lambda: None)):
    return x


def _cycle_b(x=Depends(_cycle_a)):
    return x


_cycle_a.__defaults__ = (Depends(_cycle_b),)


def test_circular_dependency_detected_at_registration():
    app = Veloce(openapi_url=None)

    with pytest.raises(ValueError, match="Circular dependency detected") as exc_info:

        @app.get("/cycle")
        def handler(x=Depends(_cycle_a)):
            return {"x": x}

    msg = str(exc_info.value)
    assert "_cycle_a" in msg
    assert "_cycle_b" in msg
    assert " -> " in msg


def test_circular_dependency_chain_distinguishes_lambdas_via_qualname():
    # Two distinct lambdas in a cycle: bare __name__ would render both as
    # `<lambda>`. __qualname__ carries the enclosing function scope
    # (`...test_..<locals>.<lambda>`) which lets the chain be read.
    app = Veloce(openapi_url=None)

    lam_a = lambda x=Depends(lambda: None): x  # noqa: E731
    lam_b = lambda x=Depends(lam_a): x  # noqa: E731
    lam_a.__defaults__ = (Depends(lam_b),)

    with pytest.raises(ValueError, match="Circular dependency detected") as exc_info:

        @app.get("/lambda-cycle")
        def handler(x=Depends(lam_a)):
            return {"x": x}

    msg = str(exc_info.value)
    # qualname for nested lambdas includes the enclosing function name and
    # the `<locals>` marker — both must appear so the two lambdas are
    # distinguishable in the rendered chain.
    assert "<locals>" in msg
    assert "test_circular_dependency_chain_distinguishes_lambdas_via_qualname" in msg
    assert msg.count("<lambda>") >= 2
