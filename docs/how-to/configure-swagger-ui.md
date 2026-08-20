---
description: Tune the Swagger UI docs page with swagger_ui_parameters and pre-fill the Authorize dialog with swagger_ui_init_oauth, both passed to Veloce.
tags: [openapi, docs, swagger, oauth2]
---

# Configure Swagger UI

Veloce serves the Swagger UI explorer at `/docs` by default. Two
[`Veloce`](../reference/application.md#veloce.Veloce) constructor arguments tune that page:
`swagger_ui_parameters` overrides the underlying `SwaggerUIBundle` options, and
`swagger_ui_init_oauth` pre-fills the OAuth2 **Authorize** dialog. Both are
plain dicts serialised straight into the page's inline script.

!!! note "CDN-served assets"
    The Swagger UI CSS and JavaScript are loaded from a public CDN
    (`cdnjs.cloudflare.com`) with Subresource Integrity hashes pinned to a
    specific Swagger UI version. The docs page therefore needs outbound network
    access from the browser. Veloce does not bundle or self-host the assets, and
    there is no `get_swagger_ui_html`-style hook to swap the asset URLs.

## Passing Swagger UI parameters

Set `swagger_ui_parameters` to a dict of `SwaggerUIBundle` options. Each
key/value is emitted into the bundle config on the docs page, *after* Veloce's
own `url`, `dom_id`, `presets`, and `layout` defaults — so your keys win on a
collision.

```python title="app.py"
from veloce import Request, Veloce

app = Veloce(
    title="Catalog API",
    version="1.0.0",
    swagger_ui_parameters={
        "defaultModelsExpandDepth": -1,  # hide the schemas section
        "docExpansion": "none",          # collapse all operations on load
        "displayRequestDuration": True,
        "filter": True,                  # show the operation filter box
    },
)


@app.get("/items")
async def items(request: Request):
    return {"items": []}


if __name__ == "__main__":
    app.run(port=8000)
```

Values are JSON-serialised, so use Python literals: `True`/`False` become
`true`/`false`, `-1` and strings pass through unchanged, and nested dicts/lists
are allowed. The keys are Swagger UI's own option names — Veloce does not
validate them, it forwards whatever you give it.

!!! note
    A few keys are owned by Veloce and overriding them breaks the page rather
    than customising it: `url` points at the generated `/openapi.json`, `dom_id`
    targets the mount node, and `presets`/`layout` wire up the standalone
    layout. Override the cosmetic and behavioural options, not these four.

## Pre-filling the Authorize dialog

When your API declares an OAuth2 [security scheme](../guide/security-schemes.md),
Swagger UI shows an **Authorize** button. Pass `swagger_ui_init_oauth` to
pre-fill that dialog so testers do not retype the client id and scopes on every
visit. The dict is handed to Swagger UI's `initOAuth(...)`.

```python title="app.py"
from veloce import Depends, OAuth2PasswordBearer, Request, Veloce

app = Veloce(
    title="Catalog API",
    version="1.0.0",
    swagger_ui_init_oauth={
        "clientId": "catalog-ui",
        "appName": "Catalog API docs",
        "scopes": "items:read items:write",
        "usePkceWithAuthorizationCodeGrant": True,
    },
)

oauth2 = OAuth2PasswordBearer(token_url="/token")


@app.get("/items")
async def items(request: Request, token: str = Depends(oauth2)):
    return {"items": []}


if __name__ == "__main__":
    app.run(port=8000)
```

The keys are Swagger UI's `initOAuth` field names (`clientId`, `scopes`,
`usePkceWithAuthorizationCodeGrant`, and so on). Veloce serialises the dict and
calls `ui.initOAuth(...)` only when you supply it; omit the argument and no
`initOAuth` call is emitted.

!!! warning "Never put a client secret here"
    `swagger_ui_init_oauth` is rendered into the docs HTML, which is public to
    anyone who can reach `/docs`. Only put a public `clientId` and non-secret
    fields here. For confidential clients, prefer PKCE
    (`usePkceWithAuthorizationCodeGrant`) over shipping a `clientSecret` to the
    browser.

## Combining both

The two arguments are independent and compose — set the UI behaviour and the
OAuth pre-fill together.

```python title="app.py"
from veloce import Request, Veloce

app = Veloce(
    title="Catalog API",
    version="1.0.0",
    swagger_ui_parameters={"docExpansion": "none", "persistAuthorization": True},
    swagger_ui_init_oauth={"clientId": "catalog-ui", "scopes": "items:read"},
)


@app.get("/items")
async def items(request: Request):
    return {"items": []}


if __name__ == "__main__":
    app.run(port=8000)
```

!!! tip
    `persistAuthorization: True` keeps an entered token across page reloads,
    which pairs well with a pre-filled Authorize dialog during local testing.

## Testing the docs page

Both settings end up as literals inside the `/docs` HTML. Render the page with
the in-memory [`TestClient`](../reference/testing.md#veloce.TestClient) and assert the
values are present.

```python
from veloce import Request, TestClient, Veloce

app = Veloce(
    swagger_ui_parameters={"docExpansion": "none"},
    swagger_ui_init_oauth={"clientId": "catalog-ui"},
)


@app.get("/ping")
async def ping(request: Request):
    return {"pong": True}


client = TestClient(app)

resp = client.get("/docs")
assert resp.status_code == 200
assert '"docExpansion":"none"' in resp.text
assert "ui.initOAuth(" in resp.text
assert '"clientId":"catalog-ui"' in resp.text
```

Disable the page entirely with `docs_url=None` — see
[Conditional OpenAPI](conditional-openapi.md) — and `/docs` returns `404`
regardless of these two arguments.

## Next steps

- Turn the docs page off in production — see [Conditional OpenAPI](conditional-openapi.md).
- Declare the schemes the Authorize dialog drives — see [Security schemes](../guide/security-schemes.md).
- Customise the document the page renders — see [OpenAPI, metadata and docs](../guide/openapi.md).
- Full signatures are in the [API reference](../reference/index.md).
