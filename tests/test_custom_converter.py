"""register_converter — custom path-converter registration."""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.routing.converters import Converter, parse_converter, register_converter
from veloce.testclient import TestClient


class SlugConverter(Converter):
    """Matches lowercase-hyphen slugs."""

    __slots__ = ()

    def match(self, value: str):
        import re

        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            return True, value
        return False, None


def test_register_and_parse_custom_converter():
    register_converter("slug", SlugConverter)
    conv = parse_converter("slug")
    assert isinstance(conv, SlugConverter)


def test_custom_converter_matches_in_route():
    register_converter("slug2", SlugConverter)
    app = Veloce()

    @app.get("/posts/{name:slug2}")
    async def post(name: str):
        return {"slug": name}

    with TestClient(app) as client:
        resp = client.get("/posts/my-first-post")
        assert resp.status_code == 200
        assert resp.json() == {"slug": "my-first-post"}


def test_custom_converter_rejects_non_match_as_route_miss():
    register_converter("slug3", SlugConverter)
    app = Veloce()

    @app.get("/x/{name:slug3}")
    async def x(name: str):
        return {}

    with TestClient(app) as client:
        # "Bad Slug" (space, caps) fails the converter → 404 route miss.
        resp = client.get("/x/Bad Slug")
        assert resp.status_code == 404


def test_register_converter_rejects_builtin_override():
    with pytest.raises(ValueError, match="built-in"):
        register_converter("int", SlugConverter)


def test_register_converter_rejects_non_converter_class():
    with pytest.raises(TypeError, match="subclass of Converter"):
        register_converter("bad", str)


def test_unknown_converter_still_raises():
    with pytest.raises(ValueError, match="unknown path converter"):
        parse_converter("definitely_not_registered")
