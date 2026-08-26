"""The OpenAPI lowering reads slotted attributes directly, not defensively.

`contrib/openapi.py` carried ~20 `getattr(info, "field", default)` and
`getattr(app, "field", default)` calls against fields that cannot be absent:
`RouteInfo` declares them in `__slots__` and assigns every one unconditionally in
`__init__`, and the `Veloce` attributes are assigned in its constructor. The
default was unreachable in every case.

That is not merely noise. A `getattr` with a default tells the next reader "this
may be missing", which is false, and it converts a genuine typo — a renamed
field, a field that stops being assigned — from an `AttributeError` at the call
site into a silent default that propagates into the generated schema. The file
already carried a comment explaining why it stopped doing this for `title` and
`version`; this finishes the job.

The tests below assert the two premises that make the removal safe, and then
that the removal happened. Together they mean a field that *becomes* optional
later fails here rather than being papered over again.
"""

from __future__ import annotations

import inspect
import re

import pytest

from veloce import Veloce
from veloce.routing.router import RouteInfo

#: The `RouteInfo` fields the lowering reads directly.
_INFO_FIELDS = (
    "path_template",
    "openapi_extra",
    "dependencies",
    "handler",
    "operation_id",
    "callbacks",
)

#: The `Veloce` attributes the lowering reads directly.
_APP_FIELDS = (
    "summary",
    "description",
    "terms_of_service",
    "contact",
    "license_info",
    "servers",
    "openapi_tags",
    "openapi_external_docs",
    "webhooks",
    "validate_openapi",
    "debug",
)


# ── premise one: the fields cannot be absent ─────────────────────────


@pytest.mark.parametrize("field", _INFO_FIELDS)
def test_the_route_field_is_slotted(field):
    assert field in RouteInfo.__slots__


@pytest.mark.parametrize("field", _INFO_FIELDS)
def test_the_route_field_is_assigned_unconditionally(field):
    """Slotted is not enough: a slot never assigned still raises on read."""
    source = inspect.getsource(RouteInfo.__init__)
    assignments = re.findall(rf"^\s*self\.{field}\s*[:=]", source, re.M)
    assert len(assignments) == 1, f"{field} is assigned {len(assignments)} times"


@pytest.mark.parametrize("field", _INFO_FIELDS)
def test_a_real_route_has_the_field(field):
    app = Veloce(openapi_url=None)

    @app.get("/r")
    async def route():
        return {}

    assert hasattr(app.match("GET", "/r").route_info, field)


@pytest.mark.parametrize("field", _APP_FIELDS)
def test_a_default_app_has_the_attribute(field):
    assert hasattr(Veloce(openapi_url=None), field)


@pytest.mark.parametrize("field", _APP_FIELDS)
def test_a_configured_app_has_the_attribute(field):
    app = Veloce(title="T", version="1", description="d", summary="s", debug=True)
    assert hasattr(app, field)


# ── premise two: the defensiveness is gone ───────────────────────────


@pytest.mark.parametrize("field", _INFO_FIELDS + _APP_FIELDS)
def test_the_lowering_does_not_guard_the_field(field):
    from veloce.contrib import openapi

    source = inspect.getsource(openapi)
    for obj in ("info", "app", "route_info"):
        assert f'getattr({obj}, "{field}"' not in source, (
            f"openapi.py guards {obj}.{field}, which cannot be missing - if it "
            f"genuinely became optional, remove it from this test's list and say why"
        )


# ── and the document is unchanged ────────────────────────────────────


def _schema(**app_kwargs):
    app = Veloce(**app_kwargs)

    @app.get("/items/{item_id}", operation_id="fetch", tags=["x"])
    async def fetch(item_id: int) -> dict:
        return {}

    return app.openapi()


def test_the_document_still_builds():
    assert _schema(title="T", version="1")["openapi"].startswith("3.")


def test_the_operation_id_is_still_honoured():
    schema = _schema(title="T", version="1")
    assert schema["paths"]["/items/{item_id}"]["get"]["operationId"] == "fetch"


def test_the_path_template_is_still_used():
    assert "/items/{item_id}" in _schema(title="T", version="1")["paths"]


def test_optional_metadata_is_still_omitted_when_unset():
    """The `or`/falsy guards that *were* load-bearing must survive."""
    info = _schema(title="T", version="1")["info"]
    assert "summary" not in info
    assert "termsOfService" not in info
    assert "contact" not in info
    assert "license" not in info


def test_optional_metadata_is_still_included_when_set():
    info = _schema(
        title="T",
        version="1",
        summary="A summary",
        terms_of_service="https://example.test/tos",
        contact={"name": "Team"},
        license_info={"name": "MIT"},
    )["info"]
    assert info["summary"] == "A summary"
    assert info["termsOfService"] == "https://example.test/tos"
    assert info["contact"] == {"name": "Team"}
    assert info["license"] == {"name": "MIT"}


def test_a_route_with_no_operation_id_still_gets_a_generated_one():
    app = Veloce(title="T", version="1")

    @app.get("/plain")
    async def plain() -> dict:
        return {}

    operation = app.openapi()["paths"]["/plain"]["get"]
    assert operation["operationId"]


def test_a_route_with_no_openapi_extra_is_unaffected():
    app = Veloce(title="T", version="1")

    @app.get("/plain")
    async def plain() -> dict:
        return {}

    assert "/plain" in app.openapi()["paths"]


def test_openapi_extra_is_still_merged():
    app = Veloce(title="T", version="1")

    @app.get("/plain", openapi_extra={"x-custom": "v"})
    async def plain() -> dict:
        return {}

    assert app.openapi()["paths"]["/plain"]["get"]["x-custom"] == "v"
