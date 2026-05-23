"""swagger_ui_parameters + swagger_ui_init_oauth passthrough (O14)."""

from __future__ import annotations

from veloce import Veloce
from veloce.testclient import TestClient


def test_default_swagger_ui_renders_without_extras():
    """No ctor kwargs → no extra UI params, no initOAuth call."""
    app = Veloce(debug=True)

    @app.get("/x")
    async def x():
        return {}

    html = TestClient(app).get("/docs").text
    assert "SwaggerUIBundle" in html
    # No initOAuth call when init_oauth wasn't set.
    assert "ui.initOAuth" not in html
    # The trailing `,` from the params placeholder is still present, but
    # JavaScript tolerates trailing commas in object literals in modern
    # browsers — `, }` with no key-value before `}` is the empty case.


def test_swagger_ui_parameters_inserted_into_bundle():
    app = Veloce(
        debug=True,
        swagger_ui_parameters={
            "defaultModelsExpandDepth": -1,
            "persistAuthorization": True,
            "docExpansion": "none",
        },
    )

    @app.get("/x")
    async def x():
        return {}

    html = TestClient(app).get("/docs").text
    # Compact form — see `test_swagger_ui_init_oauth_emitted_when_configured`.
    assert '"defaultModelsExpandDepth":-1' in html
    assert '"persistAuthorization":true' in html
    assert '"docExpansion":"none"' in html


def test_swagger_ui_init_oauth_emitted_when_configured():
    app = Veloce(
        debug=True,
        swagger_ui_init_oauth={
            "clientId": "swagger-ui-client",
            "appName": "Veloce Demo",
            "scopeSeparator": " ",
            "scopes": "read write",
        },
    )

    @app.get("/x")
    async def x():
        return {}

    html = TestClient(app).get("/docs").text
    assert "ui.initOAuth(" in html
    # `orjson.dumps` produces compact output (no space after `:`); the
    # substring check matches the on-wire form Swagger UI's JSON parser
    # consumes — whitespace inside the literal is incidental.
    assert '"clientId":"swagger-ui-client"' in html
    assert '"appName":"Veloce Demo"' in html


def test_swagger_ui_parameters_default_to_none_attribute():
    """Even when no kwarg is passed, the attributes exist (default None)."""
    app = Veloce()
    assert app.swagger_ui_parameters is None
    assert app.swagger_ui_init_oauth is None


def test_swagger_ui_with_both_params_and_oauth():
    app = Veloce(
        debug=True,
        swagger_ui_parameters={"persistAuthorization": True},
        swagger_ui_init_oauth={"clientId": "abc"},
    )

    @app.get("/x")
    async def x():
        return {}

    html = TestClient(app).get("/docs").text
    assert '"persistAuthorization":true' in html
    assert "ui.initOAuth(" in html
