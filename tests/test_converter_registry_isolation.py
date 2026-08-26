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

from veloce import Veloce
from veloce.routing.converters import Converter, register_converter
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
    from veloce.routing import converters

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
    from veloce.routing.converters import parse_converter

    assert parse_converter(name) is not None


def test_a_builtin_route_still_matches():
    app = Veloce(openapi_url=None)

    @app.get("/items/{item_id:int}")
    async def item(item_id: int):
        return {"id": item_id}

    assert TestClient(app).get("/items/7").json() == {"id": 7}


def test_registering_still_refuses_to_shadow_a_builtin():
    """Restoring must not reopen the door the registration guard closes."""
    with pytest.raises(Exception):
        register_converter("int", _ShoutConverter)
