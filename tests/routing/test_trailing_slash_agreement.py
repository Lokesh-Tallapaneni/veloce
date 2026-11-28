"""What matches and what `Allow` advertises agree about trailing slashes.

Trailing-slash strictness is a four-flag rule - `tolerant_slash`,
`trailing_slash`, `unslashed_variant`, and whether the request itself ends in a
slash - and it was written **twice**, 130 lines apart, in two different shapes:
nested `if ... return None` in `_match_tree`, and a `slash_miss` boolean
expression in `get_allowed_methods`. Nothing tied them together.

They answer two halves of one question. `_match_tree` decides whether a request
routes; `get_allowed_methods` decides what a 405's `Allow` header names. If they
disagree, a 405 either advertises a method that would not have matched, or omits
one that would - and a client believes it.

They are one `_slash_mismatch` predicate now. These tests pin the property
across every combination, so a future change to either consumer that reaches for
its own copy fails here.
"""

from __future__ import annotations

import inspect

import pytest

from veloce import Veloce
from veloce.routing import router


def _app(registered: str, *, strict: bool | None = None, both: bool = False) -> Veloce:
    app = Veloce(openapi_url=None)
    kwargs = {} if strict is None else {"strict_slashes": strict}

    @app.get(registered, **kwargs)
    async def handler():
        return {"which": registered}

    if both:
        other = registered.rstrip("/") if registered.endswith("/") else registered + "/"

        @app.post(other)
        async def other_handler():
            return {"which": other}

    return app


CASES = [
    ("/items", "/items"),
    ("/items", "/items/"),
    ("/items/", "/items"),
    ("/items/", "/items/"),
]


@pytest.mark.parametrize(("registered", "requested"), CASES)
def test_a_match_implies_the_method_is_advertised(registered, requested):
    """The property, in one direction."""
    app = _app(registered)
    matched = app.match("GET", requested) is not None
    advertised = "GET" in app.get_allowed_methods(requested)
    assert matched == advertised, (registered, requested, matched, advertised)


@pytest.mark.parametrize(("registered", "requested"), CASES)
def test_the_two_agree_under_tolerant_slashes(registered, requested):
    """`strict_slashes=False` skips the gate; both consumers must skip it."""
    app = _app(registered, strict=False)
    matched = app.match("GET", requested) is not None
    advertised = "GET" in app.get_allowed_methods(requested)
    assert matched == advertised, (registered, requested)


@pytest.mark.parametrize(("registered", "requested"), CASES)
def test_the_two_agree_when_both_forms_are_registered(registered, requested):
    """A node serving both shapes fires neither arm - for both consumers."""
    app = _app(registered, both=True)
    matched = app.match("GET", requested) is not None
    advertised = "GET" in app.get_allowed_methods(requested)
    assert matched == advertised, (registered, requested)


# ── and the rule itself is still the rule ────────────────────────────


def test_a_slashed_route_refuses_an_unslashed_request():
    """The negative: agreement is not achieved by matching everything."""
    app = _app("/items/")
    assert app.match("GET", "/items") is None
    assert "GET" not in app.get_allowed_methods("/items")


def test_an_unslashed_route_refuses_a_slashed_request():
    app = _app("/items")
    assert app.match("GET", "/items/") is None


def test_a_tolerant_route_accepts_both():
    app = _app("/items", strict=False)
    assert app.match("GET", "/items") is not None
    assert app.match("GET", "/items/") is not None


def test_root_is_never_treated_as_trailing_slash():
    """`/` is the one path whose slash is not a trailing slash."""
    app = _app("/")
    assert app.match("GET", "/") is not None
    assert "GET" in app.get_allowed_methods("/")


def test_the_predicate_is_used_by_both_consumers():
    """The structural half: neither may grow its own copy again."""
    for name in ("_match_tree", "get_allowed_methods"):
        source = inspect.getsource(getattr(router.Router, name))
        assert "_slash_mismatch(" in source, f"{name} no longer uses the shared predicate"
        # Reading the flags is what a re-grown copy looks like. A comment
        # naming one is not; `_match_tree` still explains the rule in prose.
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        for flag in (".tolerant_slash", ".unslashed_variant", ".trailing_slash"):
            assert flag not in code, f"{name} reads {flag} again - it has its own copy"
