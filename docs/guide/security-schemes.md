---
description: Authentication schemes in Veloce — HTTP Basic, Digest, Bearer, API key, and OAuth2 schemes used as dependencies, with an end-to-end OAuth2-password + JWT login flow and the Swagger Authorize button.
tags: [security, authentication, oauth2, dependency-injection]
---

# Security schemes

Veloce ships a set of authentication schemes that extract credentials from
an incoming request. Each scheme is a callable, so you use it as a
dependency with [`Depends`](../reference/dependencies.md#veloce.Depends) or
[`Security`](../reference/dependencies.md#veloce.Security) and Veloce resolves it before
your handler runs. When a credential is missing or malformed the scheme raises
[`HTTPException`](../reference/exceptions.md#veloce.HTTPException) with a `401` and the
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

[`HTTPBasic`](../reference/security.md#veloce.HTTPBasic) decodes the
`Authorization: Basic <base64>` header into an
[`HTTPBasicCredentials`](../reference/security.md#veloce.HTTPBasicCredentials) with
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

## HTTP Digest

[`HTTPDigest`](../reference/security.md#veloce.HTTPDigest) parses an
`Authorization: Digest ...` header (RFC 7616) into an
[`HTTPDigestCredentials`](../reference/security.md#veloce.HTTPDigestCredentials) — a
struct of the named fields:

| Field |
| --- |
| `username` |
| `realm` |
| `nonce` |
| `uri` |
| `response` |
| `qop` |
| `nc` |
| `cnonce` |
| `opaque` |
| `algorithm` |

When the header is missing or not a Digest credential it raises a `401` carrying a
`WWW-Authenticate: Digest realm="...", qop="...", nonce="...", algorithm=...`
challenge with a fresh random nonce.

```python
import hashlib
import secrets

from veloce import Depends, HTTPDigest, HTTPException, Veloce
from veloce.security import HTTPDigestCredentials

app = Veloce()
digest = HTTPDigest(realm="Admin area")

# HA1 = MD5/SHA-256(username:realm:password); the password never crosses
# the wire — Digest's whole point.
_HA1 = {
    "alice": hashlib.sha256(b"alice:Admin area:wonderland").hexdigest(),
}


def verify_digest(creds: HTTPDigestCredentials = Depends(digest)) -> str:
    ha1 = _HA1.get(creds.username)
    if ha1 is None:
        raise HTTPException(401, "Unknown user")
    ha2 = hashlib.sha256(f"GET:{creds.uri}".encode()).hexdigest()
    expected = hashlib.sha256(
        f"{ha1}:{creds.nonce}:{creds.nc}:{creds.cnonce}:{creds.qop}:{ha2}".encode()
    ).hexdigest()
    if not secrets.compare_digest(creds.response, expected):
        raise HTTPException(401, "Invalid digest response")
    return creds.username
```

!!! warning "Digest parses, it does not verify"
    `HTTPDigest` only parses the field list and emits the challenge. It does
    not compute or compare the response hash — your application owns the
    secret (HA1) and must compute the expected digest itself, as shown above.
    Server-side nonce replay tracking is also your responsibility. The
    constructor takes `qop` (default `"auth"`), `algorithm` (default
    `"SHA-256"`, RFC 7616 Sec. 3.2 preferred over MD5), `auto_error`, and an
    optional `nonce_factory` callable.

## HTTP Bearer

[`HTTPBearer`](../reference/security.md#veloce.HTTPBearer) extracts the token that
follows `Authorization: Bearer `. It returns the token string with no further
interpretation — decoding or validating a JWT is up to you (see
[JSON Web Tokens](#json-web-tokens) below).

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
of the request: [`APIKeyHeader`](../reference/security.md#veloce.APIKeyHeader) from a
request header, [`APIKeyQuery`](../reference/security.md#veloce.APIKeyQuery) from a
query-string parameter, and
[`APIKeyCookie`](../reference/security.md#veloce.APIKeyCookie) from a cookie. All three
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

When `auto_error` is left on (the default) and the credential is missing,
the scheme raises a `401` carrying a `WWW-Authenticate: APIKey` challenge
(RFC 9110 Sec. 11.6.1). Pass `realm="..."` to emit
`WWW-Authenticate: APIKey realm="..."` so clients know which protection
space the resource belongs to:

```python
api_key = APIKeyHeader(name="X-API-Key", realm="admin")
```

!!! warning "Keys in URLs leak"
    `APIKeyQuery` puts the key in the URL, where it lands in server access
    logs, browser history, and `Referer` headers. Prefer `APIKeyHeader`
    or `APIKeyCookie` for anything sensitive.

## OAuth2 password bearer

[`OAuth2PasswordBearer`](../reference/security.md#veloce.OAuth2PasswordBearer) is the
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
[`OAuth2PasswordRequestForm`](../reference/security.md#veloce.OAuth2PasswordRequestForm)
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

[`OAuth2PasswordRequestFormStrict`](../reference/security.md#veloce.OAuth2PasswordRequestFormStrict)
is the same form but requires `grant_type` to be present and exactly
`password`, rejecting anything else with a `422`. Use it when you want to
enforce RFC 6749 §4.3.2 strictly.

!!! note "Other OAuth2 / OIDC schemes"
    [`OAuth2AuthorizationCodeBearer`](../reference/security.md#veloce.OAuth2AuthorizationCodeBearer)
    takes `authorizationUrl`, `tokenUrl`, an optional `refreshUrl`, and
    `scopes`; [`OpenIdConnect`](../reference/security.md#veloce.OpenIdConnect) takes a
    single `openIdConnectUrl`. Both extract a Bearer token exactly like
    `OAuth2PasswordBearer` — they differ only in the OpenAPI metadata
    they advertise to interactive clients.

## Scopes with `Security`

When a token carries OAuth2 scopes, declare them with
[`Security`](../reference/dependencies.md#veloce.Security) instead of `Depends`. A
dependency that takes a [`SecurityScopes`](../reference/dependencies.md#veloce.SecurityScopes)
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

## Session (cookie) authentication

`SessionAuth` turns a signed-in cookie session into a `Principal`, so a guard
written against `current_principal()` behaves the same for a browser session as
it does for a bearer token:

```python
from veloce import Depends, Veloce, current_principal
from veloce.middleware.sessions import SessionMiddleware
from veloce.security import SessionAuth, login_session, logout_session

app = Veloce()
app.config["SECRET_KEY"] = "change-me"
app.add_middleware(SessionMiddleware, secret_key="change-me")
session_auth = SessionAuth()


@app.post("/login")
async def login(request):
    login_session(request, "user-42", scopes={"items:read"})
    return {"ok": True}


@app.get("/me")
async def me(principal=Depends(session_auth)):
    return {"user": principal.subject, "via": current_principal().subject}


@app.post("/logout")
async def logout(request):
    logout_session(request)
    return {"ok": True}
```

`login_session` rotates the session id before writing the identity, so a
session id planted before sign-in cannot be replayed against the authenticated
session. `logout_session` clears the whole session rather than only the
identity keys, so per-user state cannot survive into the next user of that
cookie.

Pass `auto_error=False` for pages that render differently when signed in, and
`loader=` to build a richer principal from the stored subject (a database
lookup) or to reject a session whose user has since been deleted.

!!! note "Added in version 0.15"
    `SessionAuth`, `login_session`, `logout_session`. Earlier versions left the
    session and the `Principal` unconnected, so a session-logged-in user
    resolved to `current_principal() is None`.

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

## JSON Web Tokens

[`encode_jwt`](../reference/security.md#veloce.encode_jwt) and
[`decode_jwt`](../reference/security.md#veloce.decode_jwt) sign and verify compact
JWTs using the HMAC-SHA2 family (`HS256`/`HS384`/`HS512`) with no external
dependency. RSA/EC algorithms and `alg: "none"` are unsupported by design;
the signature is always verified before the payload JSON is decoded.

```python
from veloce import decode_jwt, encode_jwt

token = encode_jwt({"sub": "42", "exp": 1893456000}, "secret")
claims = decode_jwt(token, "secret", algorithms=["HS256"])
claims["sub"]   # "42"
```

The `algorithms` allow-list is required — there is no default — which is
what defeats algorithm-confusion attacks. `decode_jwt` validates `exp`
and `nbf` (with optional `leeway`) and can additionally check `audience`,
`issuer`, and a `require` list of claim names. Each failure raises a
distinct subclass of [`JWTError`](../reference/security.md#veloce.JWTError):

| Subclass |
| --- |
| `ExpiredSignatureError` |
| `ImmatureSignatureError` |
| `InvalidSignatureError` |
| `InvalidAudienceError` |
| `InvalidIssuerError` |
| `MissingClaimError` |
| `UnsupportedAlgorithmError` |
| `InvalidTokenError` |

`InvalidTokenError` covers a malformed token — including one whose segments
carry a byte outside ASCII, which the base64url alphabet does not contain
(RFC 7515 §2).

!!! note "Every rejection is a `JWTError`"
    `decode_jwt` raises nothing outside this hierarchy for a malformed token, so
    the `except JWTError` shown below is sufficient — it does not need a bare
    `except Exception` behind it to be safe against a hostile token.

    !!! note "Fixed in version 0.18.0"
        A token with a non-ASCII byte in one of its segments previously raised
        `UnicodeEncodeError`, which is not a `JWTError`, so an auth dependency
        written this way did not catch it and the route answered `500` instead
        of `401`.

A successful decode returns a read-only
[`Claims`](../reference/security.md#veloce.Claims) mapping.

## End-to-end: OAuth2 password flow with JWT

The pieces above assemble into a complete login-and-protect flow with no
external dependency: a `/token` endpoint verifies a hashed password and signs
a JWT, an `OAuth2PasswordBearer` scheme extracts the token on protected
routes, a `get_current_user` dependency decodes it, and a layered
`get_current_active_user` dependency rejects disabled accounts.

```python title="app.py"
import time

from veloce import (
    Depends,
    HTTPException,
    OAuth2PasswordBearer,
    Veloce,
    decode_jwt,
    encode_jwt,
    hash_password,
    verify_password,
)
from veloce.security import OAuth2PasswordRequestForm

SECRET = "change-me-in-production"
ALGORITHM = "HS256"

app = Veloce()
oauth2 = OAuth2PasswordBearer(token_url="/token")

# A real app reads users from a database. The password is stored hashed.
_USERS = {
    "alice": {
        "username": "alice",
        "password_hash": hash_password("wonderland"),
        "disabled": False,
    },
}


@app.post("/token")
async def login(request):
    form = await OAuth2PasswordRequestForm.from_request(request)
    user = _USERS.get(form.username)
    if user is None or not verify_password(user["password_hash"], form.password):
        raise HTTPException(
            401,
            "Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = {"sub": user["username"], "exp": int(time.time()) + 3600}
    token = encode_jwt(claims, SECRET, alg=ALGORITHM)
    return {"access_token": token, "token_type": "bearer"}


def get_current_user(token: str = Depends(oauth2)) -> dict:
    try:
        claims = decode_jwt(token, SECRET, algorithms=[ALGORITHM])
    except Exception as err:
        raise HTTPException(
            401,
            "Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from err
    user = _USERS.get(claims["sub"])
    if user is None:
        raise HTTPException(401, "User no longer exists")
    return user


def get_current_active_user(user: dict = Depends(get_current_user)) -> dict:
    if user["disabled"]:
        raise HTTPException(400, "Inactive user")
    return user


@app.get("/users/me")
async def read_me(user: dict = Depends(get_current_active_user)):
    return {"username": user["username"]}


if __name__ == "__main__":
    app.run(port=8000)
```

The flow runs end-to-end in-process with the [`TestClient`](testing.md): post
form credentials to `/token`, then send the returned token as a Bearer header.

```python
from veloce import TestClient

# app from the block above
client = TestClient(app)

token_resp = client.post("/token", data={"username": "alice", "password": "wonderland"})
assert token_resp.status_code == 200
access_token = token_resp.json()["access_token"]

me = client.get("/users/me", headers={"Authorization": f"Bearer {access_token}"})
assert me.status_code == 200
assert me.json() == {"username": "alice"}

denied = client.get("/users/me", headers={"Authorization": "Bearer not-a-jwt"})
assert denied.status_code == 401
```

!!! warning "Catch `JWTError`, not bare `Exception`, in production"
    The example catches `Exception` so it stays short. In real code catch
    [`JWTError`](../reference/security.md#veloce.JWTError) (the base of every decode
    failure) so genuine bugs are not converted into a `401`. `decode_jwt`
    raises `ValueError` for a misconfigured (empty) secret — a programmer
    error you want to surface, not swallow.

!!! warning "Set a real `SECRET_KEY`"
    Sign with a high-entropy secret loaded from the environment, never a
    literal in source. Wrap it in [`Secret`](#wrapping-secrets) so it cannot
    leak through logs. Rotating the secret invalidates every outstanding
    token.

## Interactive docs and the Authorize button

When a route depends on a security scheme, Veloce's generated OpenAPI schema
gains a `components.securitySchemes` entry and the operation gains a
`security` requirement, so the Swagger UI at `/docs` renders an **Authorize**
button. The scheme that drives the password flow above emits an `oauth2`
entry whose `password` flow advertises the `tokenUrl` and `scopes` you
configured:

```json
{
  "components": {
    "securitySchemes": {
      "OAuth2PasswordBearer": {
        "type": "oauth2",
        "flows": {
          "password": {"tokenUrl": "/token", "scopes": {}}
        }
      }
    }
  }
}
```

The scheme is keyed by its class name (`OAuth2PasswordBearer`,
`HTTPBearer`, `HTTPBasic`, `HTTPDigest`, `APIKeyHeader`, ...), so reusing the
same scheme class across routes reuses one `securitySchemes` entry. Scopes
declared with [`Security`](#scopes-with-security) appear in the per-operation
`security` list; plain `Depends` requirements list an empty scope array.

Inspect the emitted schema directly with `app.openapi()`:

```python
from veloce import Depends, OAuth2PasswordBearer, Veloce

app = Veloce(title="Demo", version="1.0.0")
oauth2 = OAuth2PasswordBearer(token_url="/token")


@app.get("/users/me")
async def me(token: str = Depends(oauth2)):
    return {"token": token}


schema = app.openapi()
scheme = schema["components"]["securitySchemes"]["OAuth2PasswordBearer"]
assert scheme["type"] == "oauth2"
assert scheme["flows"]["password"]["tokenUrl"] == "/token"
assert schema["paths"]["/users/me"]["get"]["security"] == [{"OAuth2PasswordBearer": []}]
```

!!! note "Pre-filling OAuth2 credentials"
    Pass `swagger_ui_init_oauth=` to [`Veloce`](../reference/application.md#veloce.Veloce)
    to seed the Authorize dialog (for example a default `clientId`). The
    Authorize button only appears for schemes reached through `Depends` or
    `Security`; a scheme constructed but never used as a dependency emits no
    OpenAPI metadata.

## Password-reset tokens

[`make_reset_token`](../reference/security.md#veloce.make_reset_token) and
[`check_reset_token`](../reference/security.md#veloce.check_reset_token) issue
storage-free, self-invalidating password-reset links. They bind an opaque
caller-supplied state fingerprint — typically the user id plus password
hash — into a signed, expiring token built on
[`Signer`](signing.md). When the password changes (or the user logs in)
the fingerprint changes and the old token stops validating, so no
server-side record of issued tokens is needed.

```python
from veloce import check_reset_token, make_reset_token

state = b"".join([str(user.id).encode(), user.password_hash.encode()])
token = make_reset_token(state, secret=SECRET)
# ... later, from the reset link ...
if check_reset_token(token, state, secret=SECRET, max_age=3600):
    ...   # allow the reset
```

`check_reset_token` returns `False` for an invalid, expired, or
no-longer-bound token (it raises only on programmer misuse). Pass
`fallback_secrets=[...]` to keep accepting tokens signed with a rotated
previous secret.

## Wrapping secrets

[`Secret`](../reference/security.md#veloce.Secret) wraps a `str`/`bytes` secret so it
resists accidental disclosure: `repr`, `str`, f-strings, and `%`
interpolation all render `***`, and the plaintext only escapes through an
explicit `.reveal()`. Equality is constant-time, the wrapper is unhashable,
and the JSON encoder refuses to serialise it.

```python
import os

from veloce import Secret

token = Secret(os.environ["API_TOKEN"])
print(token)            # ***
send(token.reveal())    # the real value
```

## Next steps

- [Passwords](passwords.md) — hash and verify passwords for the `/token`
  endpoint with `hash_password` / `verify_password`.
- [Signing](signing.md) — issue tamper-proof, time-limited tokens as an
  alternative to JWTs.
- [Dependency injection](dependency-injection.md) — how `Depends` and
  `Security` resolve, including `yield` teardown and overrides.
- The [API reference](../reference/index.md) documents the full signature of
  every scheme.
