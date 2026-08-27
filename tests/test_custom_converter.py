"""register_converter — custom path-converter registration."""

from __future__ import annotations

import re

import pytest

from veloce import Veloce
from veloce.routing.converters import Converter, parse_converter, register_converter
from veloce.testclient import TestClient


class SlugConverter(Converter):
    """Matches lowercase-hyphen slugs."""

    __slots__ = ()

    def match(self, value: str):
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


def test_custom_converter_in_regex_forced_segment_raises_at_registration():
    # A custom converter sharing a segment with static text (`/v{name:slug4}/api`)
    # forces the regex fallback, but the converter's match() has no regex
    # representation. Registration must raise rather than miscompile the route
    # into a regex matching the literal text "slug4".
    register_converter("slug4", SlugConverter)
    app = Veloce()

    with pytest.raises(ValueError, match="custom converter 'slug4' cannot be used"):

        @app.get("/v{name:slug4}/api")
        async def handler(name: str):
            return {}


def test_custom_converter_in_multi_placeholder_segment_raises():
    # Two placeholders in one segment also force the regex fallback.
    register_converter("slug5", SlugConverter)
    app = Veloce()

    with pytest.raises(ValueError, match="custom converter 'slug5' cannot be used"):

        @app.get("/{a:slug5}.{b}")
        async def handler(a: str, b: str):
            return {}


def test_custom_converter_whole_segment_stays_radix():
    # A custom converter spanning the whole segment is a radix route, not a
    # regex route — it must register cleanly and honour converter semantics.
    register_converter("slug6", SlugConverter)
    app = Veloce()

    @app.get("/items/{name:slug6}")
    async def handler(name: str):
        return {"slug": name}

    with TestClient(app) as client:
        assert client.get("/items/my-item").status_code == 200
        assert client.get("/items/Bad Item").status_code == 404
