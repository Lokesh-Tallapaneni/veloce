"""The documentation UI - Swagger UI and ReDoc pages, and the routes serving them.

This half of `openapi.py` shares nothing with the schema generator: neither
calls a single name defined by the other. It is an HTML host - it fetches
`/openapi.json` from the browser and renders it - so what it needs is escaping
rules for embedding JSON in a `<script>` block, a CSP nonce, and two page
templates.

Keeping it in the same file as 1,840 lines of OpenAPI 3.1 lowering meant anyone
reading either had to skip the other.
"""

from __future__ import annotations

import html
from typing import Any

import orjson

from veloce.http.response import HTMLResponse, JSONResponse
from veloce.middleware.security import csp_nonce

# Swagger UI / ReDoc bundles are pinned to a specific patch version and
# loaded with a Subresource Integrity hash. Together with
# `crossorigin="anonymous"` the browser refuses to execute the script
# if the CDN ever serves bytes that do not hash to this exact digest,
# so a CDN compromise cannot inject arbitrary JavaScript onto a
# `/docs` page. Bump the versions in lock-step with the hashes - the
# hash will not match if you change one without the other.
_SWAGGER_UI_VERSION = "5.18.2"
_SWAGGER_UI_CSS_INTEGRITY = "sha512-xRGj65XGEcpPTE7Cn6ujJWokpXVLxqLxdtNZ/n1w52+76XaCRO7UWKZl9yJHvzpk99A0EP6EW+opPcRwPDxwkA=="
_SWAGGER_UI_JS_INTEGRITY = "sha512-9tBcCofqWq+PelL6USpUB7OJrCaObfefi9ht9nVZuKt1XP7eHDs7NwVljLSLVtSsErax1Tz3pG3O82eeq546Rg=="
_REDOC_VERSION = "2.1.5"
_REDOC_JS_INTEGRITY = "sha384-0GrsyTQc9Oqd8h+b2dbc4XdR2T/DYpy0tLNNstyx+LBMUyiBbcWPbEs9aRmUcaxD"

# Byte-level escapes applied to orjson output before it is embedded inline in
# a <script> block: the close-script breakout (`<`/`>`/`&`) and the U+2028 /
# U+2029 line separators (valid in JSON, but break JS string literals). The
# escapes are JSON-valid `\uXXXX` forms, so SwaggerUIBundle / JSON.parse read
# them identically.
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

    `CSPMiddleware` arms a per-request nonce; the docs templates carry it on
    every script, style, and stylesheet-link tag so a strict policy renders the
    UI without allow-listing the asset hosts (a nonced element is permitted
    regardless of its source per CSP Level 3). An app with no CSP nonce armed
    gets an empty string, leaving the markup byte-identical to before.
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
    # Where the schema route actually ends up. `app.get` prepends the router's
    # `prefix`, so a prefixed app served the schema at `<prefix><openapi_url>`
    # while both HTML templates interpolated the bare `openapi_url` - each page
    # loaded and then fetched a 404, rendering empty. `root_path` is added per
    # request rather than here, because it is a property of how the app is
    # mounted at runtime, not of how its routes were registered.
    schema_path = html.escape(f"{app.prefix}{openapi_url}" if app.prefix else openapi_url)

    def _schema_url(request: Any) -> str:
        """Return the URL a browser on `request` should fetch the schema from."""
        root = request.root_path
        return f"{html.escape(root)}{schema_path}" if root else schema_path

    # Excluded from the document they serve: `paths` describes the application's
    # API, and these three are the server's own documentation endpoints. Listed,
    # a generated client grew a method for fetching the schema it was generated
    # from and two for rendering HTML pages.
    @app.get(openapi_url, tags=["openapi"], name="openapi_schema", include_in_schema=False)
    async def openapi_schema(request: Any) -> JSONResponse:
        # Route through `app.openapi()` so a user override / customised
        # `app.openapi_schema` flows to the JSON endpoint and Swagger UI.
        return JSONResponse(app.openapi())

    async def swagger_ui(request: Any) -> HTMLResponse:
        # Render extra SwaggerUIBundle options inline as JSON literals.
        # `orjson.dumps` returns bytes, so decode for string concatenation
        # into the HTML template; the surrounding page is utf-8, so
        # orjson's raw-UTF-8 output (vs json's ensure_ascii) is fine.
        params = app.swagger_ui_parameters or {}
        if params:
            # Compact `key:value` join - orjson serialises nested values
            # without spaces, so the outer separator stays spaceless to
            # keep the rendered literal consistent throughout.
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

    # Register each interactive UI only when its URL is set - `None` or an empty
    # string disables that UI while leaving the JSON schema route in place. The
    # empty string is not a path: registered, it mounted the page at the site root.
    if docs_url:
        app.get(docs_url, tags=["openapi"], name="swagger_ui", include_in_schema=False)(swagger_ui)
    if redoc_url:
        app.get(redoc_url, tags=["openapi"], name="redoc_ui", include_in_schema=False)(redoc_ui)
