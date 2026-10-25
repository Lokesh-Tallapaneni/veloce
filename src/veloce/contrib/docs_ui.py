"""The documentation UI - Swagger UI and ReDoc pages, and the routes serving them.

An HTML host: it fetches `/openapi.json` from the browser and renders it, so
what it needs is escaping rules for embedding JSON in a `<script>` block, a CSP
nonce, and two page templates.
"""

from __future__ import annotations

import html
from typing import Any

import orjson

from veloce.http.response import HTMLResponse, JSONResponse
from veloce.middleware.security import csp_nonce

# Pinned patch versions loaded under Subresource Integrity, so a compromised CDN
# cannot inject JavaScript onto `/docs`. Bump versions and hashes in lock-step.
_SWAGGER_UI_VERSION = "5.18.2"
_SWAGGER_UI_CSS_INTEGRITY = "sha512-xRGj65XGEcpPTE7Cn6ujJWokpXVLxqLxdtNZ/n1w52+76XaCRO7UWKZl9yJHvzpk99A0EP6EW+opPcRwPDxwkA=="
_SWAGGER_UI_JS_INTEGRITY = "sha512-9tBcCofqWq+PelL6USpUB7OJrCaObfefi9ht9nVZuKt1XP7eHDs7NwVljLSLVtSsErax1Tz3pG3O82eeq546Rg=="
_REDOC_VERSION = "2.1.5"
_REDOC_JS_INTEGRITY = "sha384-0GrsyTQc9Oqd8h+b2dbc4XdR2T/DYpy0tLNNstyx+LBMUyiBbcWPbEs9aRmUcaxD"

# Close the `</script>` breakout (`<`/`>`/`&`) and the U+2028 / U+2029 separators
# that are valid JSON but break a JS string literal, as JSON-valid `\uXXXX`.
_SCRIPT_ESCAPES = (
    (b"<", b"\\u003c"),
    (b">", b"\\u003e"),
    (b"&", b"\\u0026"),
    (b"\xe2\x80\xa8", b"\\u2028"),
    (b"\xe2\x80\xa9", b"\\u2029"),
)


def _html_safe_orjson(value: Any) -> str:
    """Serialise `value` to JSON safe to embed inline in a <script> block."""
    raw = orjson.dumps(value)
    for needle, repl in _SCRIPT_ESCAPES:
        if needle in raw:
            raw = raw.replace(needle, repl)
    return raw.decode()


SWAGGER_HTML = (
    """<!DOCTYPE html>
<html>
<head>
    <title>{title} - Swagger UI</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link
      rel="stylesheet"
      href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/__SUV__/swagger-ui.min.css"
      integrity="__SUC__"
      crossorigin="anonymous"
      referrerpolicy="no-referrer"{nonce}>
</head>
<body>
    <div id="swagger-ui"></div>
    <script
      src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/__SUV__/swagger-ui-bundle.min.js"
      integrity="__SUJ__"
      crossorigin="anonymous"
      referrerpolicy="no-referrer"{nonce}></script>
    <script{nonce}>
    const ui = SwaggerUIBundle({{
        url: "{openapi_url}",
        dom_id: '#swagger-ui',
        presets: [SwaggerUIBundle.presets.apis],
        layout: "BaseLayout",
        {ui_params}
    }});
    {init_oauth}
    </script>
</body>
</html>""".replace("__SUV__", _SWAGGER_UI_VERSION)
    .replace("__SUC__", _SWAGGER_UI_CSS_INTEGRITY)
    .replace("__SUJ__", _SWAGGER_UI_JS_INTEGRITY)
)


REDOC_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>{title} - ReDoc</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet"{nonce}>
    <style{nonce}>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
    <redoc spec-url='{openapi_url}'></redoc>
    <script
      src="https://unpkg.com/redoc@__RDV__/bundles/redoc.standalone.js"
      integrity="__RDJ__"
      crossorigin="anonymous"
      referrerpolicy="no-referrer"{nonce}></script>
</body>
</html>""".replace("__RDV__", _REDOC_VERSION).replace("__RDJ__", _REDOC_JS_INTEGRITY)


def _nonce_attr(request: Any) -> str:
    """Render the CSP `nonce` attribute for the docs pages, or an empty string.

    A nonced element is permitted regardless of its source per CSP Level 3, so a
    strict policy renders the UI without allow-listing the asset hosts.
    """
    nonce = csp_nonce(request)
    return f' nonce="{html.escape(nonce, quote=True)}"' if nonce else ""


def setup_openapi_routes(
    app: Any,
    openapi_url: str = "/openapi.json",
    docs_url: str | None = "/docs",
    redoc_url: str | None = "/redoc",
) -> None:
    """Register OpenAPI schema and documentation routes.

    `docs_url` / `redoc_url` of `None` disable the Swagger UI / ReDoc UI
    respectively - the JSON schema route is still registered, so tooling
    can consume the schema without a public interactive explorer.
    """
    # `app.get` prepends the router's `prefix`, so the templates must interpolate
    # the prefixed path. `root_path` is per-request: it is a property of the mount.
    schema_path = html.escape(f"{app.prefix}{openapi_url}" if app.prefix else openapi_url)

    def _schema_url(request: Any) -> str:
        """Return the URL a browser on `request` should fetch the schema from."""
        root = request.root_path
        return f"{html.escape(root)}{schema_path}" if root else schema_path

    # Excluded from the document they serve: `paths` describes the application's
    # API, and these three are the server's own documentation endpoints.
    @app.get(openapi_url, tags=["openapi"], name="openapi_schema", include_in_schema=False)
    async def openapi_schema(request: Any) -> JSONResponse:
        # Route through `app.openapi()` so a user override / customised
        # `app.openapi_schema` flows to the JSON endpoint and Swagger UI.
        return JSONResponse(app.openapi())

    async def swagger_ui(request: Any) -> HTMLResponse:
        # Extra SwaggerUIBundle options render inline as JSON literals; the page
        # is utf-8, so orjson's raw-UTF-8 output needs no ensure_ascii.
        params = app.swagger_ui_parameters or {}
        if params:
            # orjson serialises nested values without spaces, so the outer
            # separator stays spaceless too.
            ui_params = ",".join(
                f"{_html_safe_orjson(k)}:{_html_safe_orjson(v)}" for k, v in params.items()
            )
        else:
            ui_params = ""

        oauth_init = app.swagger_ui_init_oauth
        init_oauth = f"ui.initOAuth({_html_safe_orjson(oauth_init)});" if oauth_init else ""

        html_page = SWAGGER_HTML.format(
            title=html.escape(app.title),
            openapi_url=_schema_url(request),
            ui_params=ui_params,
            init_oauth=init_oauth,
            nonce=_nonce_attr(request),
        )
        return HTMLResponse(html_page)

    async def redoc_ui(request: Any) -> HTMLResponse:
        html_page = REDOC_HTML.format(
            title=html.escape(app.title),
            openapi_url=_schema_url(request),
            nonce=_nonce_attr(request),
        )
        return HTMLResponse(html_page)

    # `None` or `""` disables that UI, leaving the JSON schema route in place;
    # `""` is not a path and would mount the page at the site root.
    if docs_url:
        app.get(docs_url, tags=["openapi"], name="swagger_ui", include_in_schema=False)(swagger_ui)
    if redoc_url:
        app.get(redoc_url, tags=["openapi"], name="redoc_ui", include_in_schema=False)(redoc_ui)
