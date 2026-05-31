---
description: Authentication schemes in Veloce — HTTP Basic, Bearer, API key, and OAuth2 schemes used as dependencies, with full login and current-user examples.
tags: [security, authentication, oauth2, dependency-injection]
---

# Security schemes

Veloce ships a set of authentication schemes that extract credentials from
an incoming request. Each scheme is a callable, so you use it as a
dependency with [`Depends`](../reference.md#veloce.Depends) or
[`Security`](../reference.md#veloce.Security) and Veloce resolves it before
your handler runs. When a credential is missing or malformed the scheme raises
[`HTTPException`](../reference.md#veloce.HTTPException) with a `401` and the
appropriate `WWW-Authenticate` header.

```python
from veloce import Depends, HTTPBearer, Veloce

app = Veloce()
bearer = HTTPBearer()


@app.get("/items")
async def list_items(token: str = Depends(bearer)):
    return {"token": token}
```

A request with `Authorization: Bearer abc123` resolves `token` to the
string `"abc123"`. A request without that header gets a `401` response
with `WWW-Authenticate: Bearer` — you never reach the handler body.

## How schemes work

Every scheme follows the same contract: construct it once at module level,
then pass the instance to `Depends(...)` in a handler parameter. The
scheme's `__call__` receives the [`Request`](requests-responses.md) and
returns the extracted credential. What it returns differs by scheme:

- `HTTPBearer`, `APIKeyHeader`, `APIKeyQuery`, `APIKeyCookie`,
  `OAuth2PasswordBearer`, `OAuth2AuthorizationCodeBearer`, and
  `OpenIdConnect` return the raw credential as a `str`.
- `HTTPBasic` returns an `HTTPBasicCredentials` object with `username`
  and `password` attributes.

Every scheme takes an `auto_error` flag (default `True`). With
`auto_error=True` a missing credential raises `401`. With
`auto_error=False` the scheme returns `None` instead, letting you treat
authentication as optional.

!!! warning "Schemes extract, they do not verify"
    A security scheme only pulls the credential off the wire. It does not
    check that a token is valid, that a password is correct, or that an
    API key exists in your store. Verification is your application's job —
    compare the credential against your user database (see
    [Passwords](passwords.md) for password verification) inside the
    handler or a dependency.

## HTTP Basic

[`HTTPBasic`](../reference.md#veloce.HTTPBasic) decodes the
`Authorization: Basic <base64>` header into an
[`HTTPBasicCredentials`](../reference.md#veloce.HTTPBasicCredentials) with
`username` and `password`. Compare the password in constant time to avoid
leaking it through timing.

```python
import secrets

from veloce import Depends, HTTPBasic, HTTPBasicCredentials, HTTPException, Veloce

app = Veloce()
basic = HTTPBasic(realm="Admin area")


def require_admin(
    credentials: HTTPBasicCredentials = Depends(basic),
) -> str:
    user_ok = secrets.compare_digest(credentials.username, "admin")
    pass_ok = secrets.compare_digest(credentials.password, "s3cret")
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Invalid credentials")
    return credentials.username


@app.get("/admin")
async def admin_panel(user: str = Depends(require_admin)):
    return {"hello": user}
```

The `realm` argument sets the realm shown in the browser's login prompt;
it is included in the `WWW-Authenticate: Basic realm="..."` header on a
`401`.

!!! warning "Basic auth sends credentials on every request"
    HTTP Basic transmits the username and password base64-encoded (not
    encrypted) on every request. Only use it over HTTPS. See the
    [OWASP Authentication cheat sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
    for guidance.

## HTTP Bearer

[`HTTPBearer`](../reference.md#veloce.HTTPBearer) extracts the token that
follows `Authorization: Bearer `. It returns the token string with no further
interpretation — decoding or validating a JWT is up to you.

```python
from veloce import Depends, HTTPBearer, HTTPException, Veloce

app = Veloce()
bearer = HTTPBearer()

# A real app would decode and verify a JWT here.
_VALID_TOKENS = {"abc123": {"id": 1, "name": "admin"}}


def get_current_user(token: str = Depends(bearer)) -> dict:
    user = _VALID_TOKENS.get(token)
    if user is None:
        raise HTTPException(401, "Invalid token")
    return user


@app.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return user
```

The `scheme_name` argument (default `"Bearer"`) changes the scheme word
accepted in the header and emitted in the challenge — pass it only if you
advertise a non-standard scheme.

## API key

The three API-key schemes read a named credential from a different part
of the request: [`APIKeyHeader`](../reference.md#veloce.APIKeyHeader) from a
request header, [`APIKeyQuery`](../reference.md#veloce.APIKeyQuery) from a
query-string parameter, and
[`APIKeyCookie`](../reference.md#veloce.APIKeyCookie) from a cookie. All three
take the parameter `name` and an optional `auto_error`, and return the key as
a `str`.

```python
from veloce import APIKeyHeader, Depends, HTTPException, Veloce

app = Veloce()
api_key = APIKeyHeader(name="X-API-Key")

_VALID_KEYS = {"key-one", "key-two"}


def require_key(key: str = Depends(api_key)) -> str:
    if key not in _VALID_KEYS:
        raise HTTPException(401, "Invalid API key")
    return key


@app.get("/data")
async def data(key: str = Depends(require_key)):
    return {"ok": True}
```

Header lookup is case-insensitive, so `X-API-Key` matches `x-api-key` on
the wire. Query and cookie lookups are case-sensitive.

!!! warning "Keys in URLs leak"
    `APIKeyQuery` puts the key in the URL, where it lands in server access
    logs, browser history, and `Referer` headers. Prefer `APIKeyHeader`
    or `APIKeyCookie` for anything sensitive.

## OAuth2 password bearer

[`OAuth2PasswordBearer`](../reference.md#veloce.OAuth2PasswordBearer) is the
scheme for the resource-owner password flow. It extracts the same
`Authorization: Bearer ` token as `HTTPBearer`, but it also records the token
URL and scopes for OpenAPI so interactive clients know where to obtain a token.

```python
from veloce import Depends, HTTPException, OAuth2PasswordBearer, Veloce

app = Veloce()
oauth2 = OAuth2PasswordBearer(token_url="/token")

_VALID_TOKENS = {"abc123": {"sub": "alice"}}


def get_current_user(token: str = Depends(oauth2)) -> dict:
    claims = _VALID_TOKENS.get(token)
    if claims is None:
        raise HTTPException(401, "Invalid token")
    return claims


@app.get("/users/me")
async def read_me(user: dict = Depends(get_current_user)):
    return user
```

### Parsing the token request

A `/token` endpoint reads a standard OAuth2 password-grant form.
[`OAuth2PasswordRequestForm`](../reference.md#veloce.OAuth2PasswordRequestForm)
parses it for you; instantiate it from the request with its `from_request`
classmethod. It exposes `username`, `password`, `scope`, `client_id`,
`client_secret`, and `grant_type`.

```python
import secrets

from veloce import HTTPException, Request, Veloce
from veloce.security import OAuth2PasswordRequestForm

app = Veloce()

_USERS = {"alice": "wonderland"}


@app.post("/token")
async def issue_token(request: Request):
    form = await OAuth2PasswordRequestForm.from_request(request)
    expected = _USERS.get(form.username)
    if expected is None or not secrets.compare_digest(form.password, expected):
        raise HTTPException(400, "Incorrect username or password")
    # A real app signs a token here; see the Signing guide.
    return {"access_token": form.username, "token_type": "bearer"}
```

[`OAuth2PasswordRequestFormStrict`](../reference.md#veloce.OAuth2PasswordRequestFormStrict)
is the same form but requires `grant_type` to be present and exactly
`password`, rejecting anything else with a `422`. Use it when you want to
enforce RFC 6749 §4.3.2 strictly.

!!! note "Other OAuth2 / OIDC schemes"
    [`OAuth2AuthorizationCodeBearer`](../reference.md#veloce.OAuth2AuthorizationCodeBearer)
    takes `authorizationUrl`, `tokenUrl`, an optional `refreshUrl`, and
    `scopes`; [`OpenIdConnect`](../reference.md#veloce.OpenIdConnect) takes a
    single `openIdConnectUrl`. Both extract a Bearer token exactly like
    `OAuth2PasswordBearer` — they differ only in the OpenAPI metadata
    they advertise to interactive clients.

## Scopes with `Security`

When a token carries OAuth2 scopes, declare them with
[`Security`](../reference.md#veloce.Security) instead of `Depends`. A
dependency that takes a [`SecurityScopes`](../reference.md#veloce.SecurityScopes)
parameter receives the union of all scopes requested between the route
and that point in the dependency graph, available as `.scopes` (a list)
and `.scope_str` (the space-joined string from RFC 6749 §3.3).

```python
from veloce import (
    Depends,
    HTTPException,
    OAuth2PasswordBearer,
    Security,
    SecurityScopes,
    Veloce,
)

app = Veloce()
oauth2 = OAuth2PasswordBearer(
    token_url="/token",
    scopes={"items:read": "Read items", "items:write": "Write items"},
)

# Maps a fake token to the scopes it was granted.
_TOKEN_SCOPES = {"abc123": {"items:read"}}


def get_current_user(
    security_scopes: SecurityScopes,
    token: str = Depends(oauth2),
) -> dict:
    granted = _TOKEN_SCOPES.get(token)
    if granted is None:
        raise HTTPException(401, "Invalid token")
    for required in security_scopes.scopes:
        if required not in granted:
            raise HTTPException(
                403,
                "Not enough permissions",
                headers={
                    "WWW-Authenticate": f'Bearer scope="{security_scopes.scope_str}"'
                },
            )
    return {"token": token, "scopes": list(granted)}


@app.get("/items")
async def read_items(
    user: dict = Security(get_current_user, scopes=["items:read"]),
):
    return {"user": user}


@app.post("/items")
async def create_item(
    user: dict = Security(get_current_user, scopes=["items:write"]),
):
    return {"created": True}
```

Each route names the scopes it needs in its `Security(..., scopes=[...])`
call. The shared `get_current_user` dependency reads those scopes from
`security_scopes` and checks them against what the token actually carries.

## Optional authentication

Pass `auto_error=False` to make a scheme return `None` instead of raising
when the credential is absent. This is useful for endpoints that behave
differently for anonymous and authenticated callers.

```python
from veloce import Depends, HTTPBearer, Veloce

app = Veloce()
bearer = HTTPBearer(auto_error=False)


@app.get("/feed")
async def feed(token: str | None = Depends(bearer)):
    if token is None:
        return {"feed": "public"}
    return {"feed": "personalized", "token": token}
```

## Testing a protected route

The in-memory [`TestClient`](testing.md) lets you exercise a scheme
without a network. Send the credential header and assert on the response.

```python
from veloce import Depends, HTTPBearer, TestClient, Veloce

app = Veloce()
bearer = HTTPBearer()


@app.get("/secure")
async def secure(token: str = Depends(bearer)):
    return {"token": token}


client = TestClient(app)

unauthorized = client.get("/secure")
assert unauthorized.status_code == 401

ok = client.get("/secure", headers={"Authorization": "Bearer abc123"})
assert ok.status_code == 200
assert ok.json() == {"token": "abc123"}
```

## Next steps

- [Passwords](passwords.md) — hash and verify passwords for your login
  endpoint.
- [Signing](signing.md) — issue tamper-proof, time-limited tokens to hand
  back from `/token`.
- [Dependency injection](dependency-injection.md) — how `Depends` and
  `Security` resolve, including `yield` teardown and overrides.
- The [API reference](../reference.md) documents the full signature of
  every scheme.
