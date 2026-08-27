"""A registered converter does not leak out of the test that registered it.

`register_converter` writes into a process-global `_CUSTOM` dict with no
teardown and no `unregister_converter`, so every registration survived for the
rest of the session. The suite compensated by hand-numbering names - `slug`,
`slug2`, `slug3` - which is a convention the next author has to know about and
which fails silently the moment two modules pick the same number.

`conftest._isolate_custom_converters` now snapshots and restores the registry
per test. These tests prove the isolation actually holds, in both directions:
a registration is visible inside its own test, and gone from the next.
"""

from __future__ import annotations

import pytest

import veloce
from veloce import Veloce, register_converter, unregister_converter
from veloce.routing import converters
from veloce.routing.converters import Converter, StringConverter, parse_converter
from veloce.testclient import TestClient


class _ShoutConverter(Converter):
    """Matches an all-caps word and lowercases it."""

    __slots__ = ()

    def match(self, value: str):
        import re

        if re.fullmatch(r"[A-Z]+", value):
            return True, value.lower()
        return False, None


def _registry() -> dict:

    return converters._CUSTOM


# ── the registration works inside its own test ───────────────────────


def test_a_registered_converter_is_usable():
    register_converter("shout", _ShoutConverter)
    assert "shout" in _registry()

    app = Veloce(openapi_url=None)

    @app.get("/{word:shout}")
    async def echo(word: str):
        return {"word": word}

    assert TestClient(app).get("/HELLO").json() == {"word": "hello"}


# ── and is gone from the next ────────────────────────────────────────
#
# These two run in file order under `-p no:randomly`, and the suite is
# randomised otherwise - so the assertion is written to hold either way: the
# name must not be present *unless this test registered it*.


def test_the_registration_did_not_leak():
    """The defect: without the fixture, `shout` is still here."""
    assert "shout" not in _registry()


def test_a_second_registration_of_the_same_name_is_fine():
    """The numbered-name workaround exists because this used to collide."""
    register_converter("shout", _ShoutConverter)
    assert _registry()["shout"] is _ShoutConverter


def test_and_that_one_did_not_leak_either():
    assert "shout" not in _registry()


# ── the built-ins are untouched ──────────────────────────────────────
#
# The negative: a restore that cleared the wrong dict, or too much of it, would
# take the built-in converters with it.


@pytest.mark.parametrize("name", ["int", "str", "float", "uuid", "path"])
def test_a_builtin_converter_still_resolves(name):
    assert parse_converter(name) is not None


def test_a_builtin_route_still_matches():
    app = Veloce(openapi_url=None)

    @app.get("/items/{item_id:int}")
    async def item(item_id: int):
        return {"id": item_id}

    assert TestClient(app).get("/items/7").json() == {"id": 7}


def test_registering_still_refuses_to_shadow_a_builtin():
    """Restoring must not reopen the door the registration guard closes."""
    with pytest.raises(ValueError, match="built-in"):
        register_converter("int", _ShoutConverter)


class TestUnregisterConverter:
    """`register_converter` finally has the public inverse its sibling ships.

    `register_encoder` / `unregister_encoder` ship both halves; the converter
    registry shipped only the writer, so the only way to undo a registration was
    to reach into the private `_CUSTOM` dict - which this module's own isolation
    fixture had to do.
    """

    @staticmethod
    def _converter_class():
        class Custom(StringConverter):
            pass

        return Custom

    def test_a_registered_converter_can_be_removed(self):
        register_converter("t-removable", self._converter_class())
        assert parse_converter("t-removable") is not None
        unregister_converter("t-removable")
        with pytest.raises(ValueError, match="t-removable"):
            parse_converter("t-removable")

    def test_removing_an_unknown_name_is_a_no_op(self):
        unregister_converter("t-never-registered")
        unregister_converter("t-never-registered")

    def test_a_builtin_cannot_be_removed(self):
        with pytest.raises(ValueError, match="built-in"):
            unregister_converter("int")

    def test_a_route_registered_earlier_keeps_working(self):
        """Documented: `parse_converter` runs at registration, not at match."""
        register_converter("t-sticky", self._converter_class())
        app = Veloce(openapi_url=None)

        @app.get("/x/{value:t-sticky}")
        async def handler(value: str):
            return {"value": value}

        unregister_converter("t-sticky")
        with TestClient(app) as client:
            assert client.get("/x/abc").json() == {"value": "abc"}

    def test_it_is_reachable_from_the_top_level(self):

        assert "unregister_converter" in veloce.__all__
        assert veloce.unregister_converter.__module__ == "veloce.routing.converters"
