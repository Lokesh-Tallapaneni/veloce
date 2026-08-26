"""url_for validates each substituted value through the route's converter.

A reversed URL must round-trip through the matcher: url_for rejects a value the
radix tree would never match, so a bad call fails at call time instead of
emitting a dead link. The router raises ``ValueError``; the ``Veloce`` app wraps
that into ``BuildError`` (its existing reverse-failure contract).
"""

from __future__ import annotations

import uuid

import pytest

from veloce import Veloce
from veloce.exceptions import BuildError
from veloce.routing import Router, register_converter
from veloce.routing.converters import Converter


def _router() -> Router:
    router = Router()

    @router.get("/items/{id:int}", name="item")
    async def item(id: int):
        return {}

    @router.get("/obj/{oid:uuid}", name="obj")
    async def obj(oid: uuid.UUID):
        return {}

    @router.get("/files/{p:path}", name="file")
    async def file(p: str):
        return {}

    @router.get("/c/{color:any(red,blue)}", name="color")
    async def color(color: str):
        return {}

    @router.get("/u/{slug}", name="user")
    async def user(slug: str):
        return {}

    return router


# ── Valid values round-trip ──────────────────────────────────────────


def test_int_converter_accepts_valid_value():
    router = _router()
    assert router.url_for("item", id=42) == "/items/42"
    assert router.url_for("item", id="42") == "/items/42"


def test_uuid_converter_round_trips():
    router = _router()
    u = uuid.uuid4()
    assert router.url_for("obj", oid=u) == f"/obj/{u}"


def test_any_converter_accepts_choice():
    router = _router()
    assert router.url_for("color", color="red") == "/c/red"


def test_path_converter_accepts_slashes():
    router = _router()
    assert router.url_for("file", p="a/b/c.txt") == "/files/a/b/c.txt"


def test_bare_param_skips_validation():
    router = _router()
    # No converter on `{slug}`; any stringifiable value is accepted.
    assert router.url_for("user", slug="a b") == "/u/a b"


def test_validation_does_not_break_query_extras():
    router = _router()
    assert router.url_for("item", id=7, page=2) == "/items/7?page=2"


def test_validation_after_value_str_coercion():
    router = _router()
    assert router.url_for("item", id=0) == "/items/0"


# ── Invalid values are rejected at call time ─────────────────────────


def test_int_converter_rejects_non_numeric():
    router = _router()
    with pytest.raises(ValueError, match="path parameter 'id'"):
        router.url_for("item", id="abc")


def test_int_converter_rejects_float_string():
    router = _router()
    with pytest.raises(ValueError):
        router.url_for("item", id="4.2")


def test_uuid_converter_rejects_bad_value():
    router = _router()
    with pytest.raises(ValueError, match="path parameter 'oid'"):
        router.url_for("obj", oid="not-a-uuid")


def test_any_converter_rejects_non_choice():
    router = _router()
    with pytest.raises(ValueError, match="path parameter 'color'"):
        router.url_for("color", color="green")


def test_custom_converter_validates_on_reverse():
    class HexConverter(Converter):
        __slots__ = ()

        def match(self, value: str):
            ok = bool(value) and all(c in "0123456789abcdef" for c in value)
            return ok, value

    register_converter("hex2", HexConverter)
    router = Router()

    @router.get("/h/{token:hex2}", name="hexroute")
    async def h(token: str):
        return {}

    assert router.url_for("hexroute", token="deadbeef") == "/h/deadbeef"
    with pytest.raises(ValueError, match="path parameter 'token'"):
        router.url_for("hexroute", token="XYZ")


# ── App-level wrapping into BuildError ───────────────────────────────


def test_app_wraps_invalid_value_in_build_error():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/items/{id:int}", name="item")
    async def item(id: int):
        return {}

    assert app.url_for("item", id=5) == "/items/5"
    with pytest.raises(BuildError) as exc:
        app.url_for("item", id="abc")
    assert isinstance(exc.value.__cause__, ValueError)


# ── A reversed URL must be one this router can match ─────────────────
#
# `url_for` validates each typed placeholder through the same converter the
# matcher applies, so a reversed URL is guaranteed to resolve. A
# segment-bounded converter never sees a `/` when matching - the path splitter
# has already cut on it - so its `match` has no reason to test for one, and
# `StringConverter` does not. Reversing tested with `match` alone, so
# `url_for("typed", name="a/b")` returned `/b/a/b`: a URL this router cannot
# match, emitted by the function whose job is to guarantee it can.
#
# The test belongs on the reverse path, not in `match`: adding it there would
# put a scan on every parameterised match to fix a URL-building bug.


def _slash_app():

    app = Veloce(openapi_url=None)

    @app.get("/b/{name:str}", name="typed")
    async def typed(name: str):
        return {"n": name}

    @app.get("/p/{rest:path}", name="greedy")
    async def greedy(rest: str):
        return {"r": rest}

    return app


def test_a_slash_in_a_segment_bounded_value_is_refused():
    """The defect: it returned a URL its own router answers 404 for."""
    app = _slash_app()
    # The app wraps a reverse failure as `BuildError`, as it does for any other
    # invalid value.
    with pytest.raises(BuildError):
        app.url_for("typed", name="a/b")


def test_a_value_without_a_slash_is_still_built():
    app = _slash_app()
    assert app.url_for("typed", name="a-b") == "/b/a-b"


def test_a_greedy_converter_still_accepts_slashes():
    """`path` legitimately crosses segments; the guard must not catch it."""
    app = _slash_app()
    url = app.url_for("greedy", rest="a/b/c")
    assert url == "/p/a/b/c"


def test_every_built_url_resolves():
    """The property `url_for` exists to guarantee."""
    from veloce.testclient import TestClient

    app = _slash_app()
    client = TestClient(app)
    for name, params in (("typed", {"name": "a-b"}), ("greedy", {"rest": "a/b/c"})):
        assert client.get(app.url_for(name, **params)).status_code == 200, name
