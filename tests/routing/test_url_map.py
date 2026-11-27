"""Veloce.url_map iterable of URL rules."""

from __future__ import annotations

from veloce import Veloce
from veloce.app import URLRule


def _make_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {}

    @app.route("/items", methods=["GET", "POST"])
    async def items():
        return {}

    @app.get("/users/{id}", name="user_detail")
    async def user_detail(id: str):
        return {}

    return app


def test_url_map_iteration_yields_rule_objects():
    app = _make_app()
    rules = list(app.url_map)
    assert all(isinstance(r, URLRule) for r in rules)
    assert len(rules) == 3


def test_url_map_groups_multi_method_routes():
    """A route registered for GET+POST shows up as one rule."""
    app = _make_app()
    items_rules = [r for r in app.url_map if r.rule == "/items"]
    assert len(items_rules) == 1
    assert sorted(items_rules[0].methods) == ["GET", "POST"]


def test_url_map_lookup_by_endpoint():
    app = _make_app()
    matches = app.url_map["user_detail"]
    assert len(matches) == 1
    assert matches[0].rule == "/users/{id}"


def test_url_map_lookup_unknown_endpoint_is_empty():
    assert _make_app().url_map["does_not_exist"] == []


def test_url_map_lookup_returns_fresh_list():
    # Mutating a returned list must not corrupt the endpoint index.
    url_map = _make_app().url_map
    url_map["user_detail"].clear()
    assert len(url_map["user_detail"]) == 1


def test_url_map_len_counts_unique_rules():
    app = _make_app()
    assert len(app.url_map) == 3


def test_url_rule_iter_tuple_unpacks():
    """`for rule, methods, endpoint in app.url_map:` works."""
    app = _make_app()
    seen = []
    for rule, methods, endpoint in app.url_map:
        seen.append((rule, sorted(methods), endpoint))
    paths = {s[0] for s in seen}
    assert "/" in paths
    assert "/items" in paths
    assert "/users/{id}" in paths


def test_url_map_repr_summarises():
    app = _make_app()
    assert repr(app.url_map) == "<URLMap with 3 rules>"
