"""`default_factory` for parameter markers - per-request fresh defaults.

A marker may carry a `default_factory` callable instead of a static
`default`. On the missing-value branch the factory runs once per request so
each request gets an independent object, avoiding the shared-mutable aliasing
a static `Query(default=[])` causes. Static defaults still snapshot inline -
the factory is invoked only for factory-backed slots, and only when the value
is actually missing.
"""

from __future__ import annotations

import warnings

import pytest

from veloce import Query, Veloce
from veloce._handler_plan import build_plan
from veloce._resolver_codegen import compile_param_resolver
from veloce.dependency import RequestValidationError, _coerce_value
from veloce.routing.params import Header, ParamBase
from veloce.testclient import TestClient

# ── Marker-level behaviour ────────────────────────────────────────────


def test_factory_runs_each_call_and_yields_distinct_objects():
    marker = Query(default_factory=list)
    a = marker.resolve_default()
    b = marker.resolve_default()
    assert a == [] and b == []
    # Each resolve builds a fresh object - no shared aliasing.
    assert a is not b


def test_static_default_is_returned_as_is():
    sentinel = object()
    marker = Query(default=sentinel)
    assert marker.resolve_default() is sentinel
    assert marker.has_default is True


def test_factory_makes_marker_report_has_default():
    assert Query(default_factory=dict).has_default is True
    assert Query().has_default is False


def test_default_and_factory_are_mutually_exclusive():
    with pytest.raises(ValueError, match="mutually exclusive"):
        Query(default=[], default_factory=list)


# ── End-to-end through the interpreter and compiled paths ─────────────


def test_query_factory_default_is_per_request_via_handler():
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def handler(tags: list[str] = Query(default_factory=list)) -> dict:
        # Mutating the injected default must not leak into the next request.
        tags.append("x")
        return {"tags": tags}

    client = TestClient(app)
    first = client.get("/items").json()
    second = client.get("/items").json()
    # If the default were shared, the second request would see ["x", "x"].
    assert first == {"tags": ["x"]}
    assert second == {"tags": ["x"]}


def test_factory_not_called_when_value_present():
    calls = 0

    def factory() -> str:
        nonlocal calls
        calls += 1
        return "fallback"

    app = Veloce(openapi_url=None)

    @app.get("/q")
    async def handler(q: str = Query(default_factory=factory)) -> dict:
        return {"q": q}

    client = TestClient(app)
    # Value supplied: the factory must not run.
    assert client.get("/q?q=given").json() == {"q": "given"}
    assert calls == 0
    # Value missing: the factory runs exactly once.
    assert client.get("/q").json() == {"q": "fallback"}
    assert calls == 1


def test_header_factory_default():
    app = Veloce(openapi_url=None)

    @app.get("/h")
    async def handler(x_token: str = Header(default_factory=lambda: "anon")) -> dict:
        return {"token": x_token}

    client = TestClient(app)
    assert client.get("/h").json() == {"token": "anon"}
    assert client.get("/h", headers={"x-token": "real"}).json() == {"token": "real"}


def test_compiled_resolver_uses_factory_each_request():
    """The compiled fast path must also build a fresh object per request."""

    def handler(tags: list[str] = Query(default_factory=list)):
        return tags

    resolver = compile_param_resolver(build_plan(handler), _coerce_value, RequestValidationError)
    assert resolver is not None

    class _Req:
        class query_params:
            @staticmethod
            def getlist(_name):
                return []

    out_a = resolver(_Req(), {})["tags"]
    out_b = resolver(_Req(), {})["tags"]
    assert out_a == [] and out_b == []
    assert out_a is not out_b


def test_compiled_resolver_snapshots_static_default():
    """A static default keeps zero-call behaviour - the same object each time."""
    sentinel = ("a",)

    def handler(q: str = Query(default=sentinel)):  # type: ignore[assignment]
        return q

    resolver = compile_param_resolver(build_plan(handler), _coerce_value, RequestValidationError)
    assert resolver is not None

    class _Req:
        class query_params:
            @staticmethod
            def get(_name):
                return None

    # Static default is snapshotted inline: the identical object is returned.
    assert resolver(_Req(), {})["q"] is sentinel


# ── Shared-mutable lint ───────────────────────────────────────────────


def test_static_mutable_default_warns_at_registration():
    def handler(tags: list[str] = Query(default=[])):
        return tags

    with pytest.warns(UserWarning, match="default_factory=list"):
        build_plan(handler)


def test_factory_default_does_not_warn():
    def handler(tags: list[str] = Query(default_factory=list)):
        return tags

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        build_plan(handler)


def test_immutable_static_default_does_not_warn():
    def handler(q: str = Query(default="")):
        return q

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        build_plan(handler)


def test_param_base_accepts_factory_directly():
    # The factory lives on the base, so every marker subclass inherits it.
    assert isinstance(ParamBase(default_factory=set), ParamBase)
    assert ParamBase(default_factory=set).resolve_default() == set()


# ── Plain (non-marker) mutable defaults ───────────────────────────────


def test_plain_list_default_is_per_request_via_handler():
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def handler(tags: list[str] = []) -> dict:
        # A bare `= []` default must not be shared across requests.
        tags.append("x")
        return {"tags": tags}

    client = TestClient(app)
    first = client.get("/items").json()
    second = client.get("/items").json()
    # If the default were shared by identity, the second request would see
    # ["x", "x"]; the registration-time copying factory keeps them isolated.
    assert first == {"tags": ["x"]}
    assert second == {"tags": ["x"]}


def test_plain_scalar_mutable_default_is_per_request():
    app = Veloce(openapi_url=None)

    @app.get("/cfg")
    async def handler(opts: dict = {}) -> dict:
        opts["seen"] = opts.get("seen", 0) + 1
        return opts

    client = TestClient(app)
    assert client.get("/cfg").json() == {"seen": 1}
    assert client.get("/cfg").json() == {"seen": 1}


def test_plain_list_default_warns_at_registration():
    def handler(tags: list[str] = []):
        return tags

    with pytest.warns(UserWarning, match="default_factory=list"):
        build_plan(handler)


def test_plain_scalar_dict_default_warns_at_registration():
    def handler(opts: dict = {}):
        return opts

    with pytest.warns(UserWarning, match="default_factory=dict"):
        build_plan(handler)


def test_plain_immutable_scalar_default_does_not_warn():
    def handler(q: str = ""):
        return q

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        build_plan(handler)


# ── OpenAPI ───────────────────────────────────────────────────────────


def test_factory_param_is_optional_with_no_static_default_in_schema():
    app = Veloce(title="t", version="1")

    @app.get("/q")
    async def handler(tags: list = Query(default_factory=list)) -> dict:
        return {"tags": tags}

    schema = app.openapi()
    params = schema["paths"]["/q"]["get"]["parameters"]
    tags_param = next(p for p in params if p["name"] == "tags")
    # A factory-backed param is optional but advertises no static default.
    assert tags_param["required"] is False
    assert "default" not in tags_param.get("schema", {})
