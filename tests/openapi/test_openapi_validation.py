"""Opt-in structural validation of the assembled OpenAPI document."""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.openapi import _validate_document

#: The component this points at is never registered, which is the whole subject
#: of the module - so it is written once. Pasted per test, a change to the shape
#: reaches some tests and not others, and the ones it misses stop testing a
#: dangling ref while still passing.
_GHOST_REF = "#/components/schemas/Ghost"


def _dangling_ref() -> dict:
    """A route's `openapi_extra` declaring a response schema that does not exist."""
    return {
        "responses": {"200": {"content": {"application/json": {"schema": {"$ref": _GHOST_REF}}}}}
    }


def test_validation_off_by_default_in_production() -> None:
    app = Veloce()  # debug=False, validate_openapi=None

    @app.get(
        "/bad",
        openapi_extra=_dangling_ref(),
    )
    async def bad(request):
        return {}

    # No validation -> the dangling ref ships rather than raising at build time.
    schema = app.openapi()
    assert "/bad" in schema["paths"]


def test_validation_follows_debug_flag() -> None:
    app = Veloce(debug=True)

    @app.get(
        "/bad",
        openapi_extra=_dangling_ref(),
    )
    async def bad(request):
        return {}

    with pytest.raises(ValueError, match="unresolved schema"):
        app.openapi()


def test_validation_explicit_opt_in() -> None:
    app = Veloce(validate_openapi=True)

    @app.get(
        "/bad",
        openapi_extra=_dangling_ref(),
    )
    async def bad(request):
        return {}

    with pytest.raises(ValueError, match="GET /bad"):
        app.openapi()


def test_validation_rejects_missing_responses() -> None:
    # The structural checker rejects an operation with no responses, a malformed
    # parameter, or a non-object operations container.
    doc = {
        # `info` is required, and the checker now says so - supply a valid one so
        # this test still exercises the responses check it was written for.
        "info": {"title": "t", "version": "1"},
        "paths": {"/x": {"get": {"summary": "x", "responses": {}}}},
        "components": {"schemas": {}},
    }
    with pytest.raises(ValueError, match="at least one response"):
        _validate_document(doc)


def test_validation_rejects_parameter_without_name_or_location() -> None:
    doc = {
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "responses": {"200": {"description": "ok"}},
                    "parameters": [{"schema": {"type": "string"}}],
                }
            }
        },
        "components": {"schemas": {}},
    }
    with pytest.raises(ValueError, match="needs `name` and `in`"):
        _validate_document(doc)


def test_validation_passes_for_well_formed_document() -> None:
    app = Veloce(validate_openapi=True)

    @app.get("/ok")
    async def ok(request, q: int = 0):
        return {}

    # A normal document validates cleanly and is returned.
    schema = app.openapi()
    assert schema["openapi"].startswith("3.1")


def test_validation_explicit_false_overrides_debug() -> None:
    app = Veloce(debug=True, validate_openapi=False)

    @app.get(
        "/bad",
        openapi_extra=_dangling_ref(),
    )
    async def bad(request):
        return {}

    # Explicit False wins over debug=True: no validation, no raise.
    assert "/bad" in app.openapi()["paths"]
