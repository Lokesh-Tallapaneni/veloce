---
description: Control when a security scheme rejects a request with 401 vs 403 — the auto_error flag on HTTPBasic / HTTPBearer / OAuth2PasswordBearer / the API-key schemes, and raising a custom HTTPException for authorization failures.
tags: [security, auth, status-codes, errors]
---

# 401 vs 403 auth errors

Every built-in security scheme — [`HTTPBasic`](../reference.md#veloce.HTTPBasic),
[`HTTPBearer`](../reference.md#veloce.HTTPBearer),
[`OAuth2PasswordBearer`](../reference.md#veloce.OAuth2PasswordBearer), and the
API-key schemes — rejects a missing or malformed credential with **401
Unauthorized** and a `WWW-Authenticate` challenge header.

Veloce never returns 403 for absent authentication.

A 403 is something your handler raises once it knows *who* the caller is but
decides they may not perform the action.

This page covers when each status fires and how to control it.

## What the schemes do by default

When a scheme is wired in with [`Security`](../reference.md#veloce.Security) (or
[`Depends`](../reference.md#veloce.Depends)) and the credential is missing or
malformed, the scheme raises an [`HTTPException`](../reference.md#veloce.HTTPException)
with status **401** before your handler runs.

```python title="app.py"
from veloce import Security, TestClient, Veloce
from veloce.security import HTTPBearer

app = Veloce()
bearer = HTTPBearer()


@app.get("/me")
async def me(token: str = Security(bearer)):
    return {"token": token}


client = TestClient(app)

# No Authorization header -> 401 with a challenge.
resp = client.get("/me")
assert resp.status_code == 401
assert resp.json()["detail"] == "Not authenticated"
assert resp.headers["www-authenticate"] == "Bearer"

# A well-formed bearer credential passes through.
ok = client.get("/me", headers={"Authorization": "Bearer abc123"})
assert ok.status_code == 200
assert ok.json() == {"token": "abc123"}
```

!!! warning "Veloce raises 401, not 403, for missing credentials"
    Older FastAPI versions returned **403 Forbidden** when a bearer or API-key
    credential was absent. Veloce always returns **401 Unauthorized** with a
    `WWW-Authenticate` header, matching [RFC 9110 §11.6.1](https://www.rfc-editor.org/rfc/rfc9110#section-11.6.1).
    If you ported a client that treats 403 as "log in again", switch it to 401.

## 401 vs 403: which to use

| Status | Meaning | Who raises it |
| --- | --- | --- |
| `401` | The request lacks valid authentication. | The scheme, automatically (or you, re-prompting for credentials). |
| `403` | The caller is authenticated but not allowed to do this. | Your handler or dependency, explicitly. |

A 401 says *"I do not know who you are"*; a 403 says *"I know who you are, and
the answer is no"*. Reserve 403 for authorization decisions made *after*
identity is established.

## Turning the automatic 401 off with `auto_error`

Every scheme takes `auto_error` (default `True`). Set `auto_error=False` and the
scheme returns `None` instead of raising when the credential is absent, so your
handler can decide what to do — serve a public response, or raise its own error.

```python title="app.py"
from veloce import Security, TestClient, Veloce
from veloce.security import HTTPBearer

app = Veloce()
optional_bearer = HTTPBearer(auto_error=False)


@app.get("/feed")
async def feed(token: str | None = Security(optional_bearer)):
    if token is None:
        return {"feed": "public"}
    return {"feed": "personalised"}


client = TestClient(app)

assert client.get("/feed").json() == {"feed": "public"}
assert client.get(
    "/feed", headers={"Authorization": "Bearer abc123"}
).json() == {"feed": "personalised"}
```

!!! note
    With `auto_error=False` the scheme also returns `None` for a malformed
    credential, not just an absent one. The handler sees `None` and cannot tell
    the two apart, so use this only where unauthenticated access is genuinely
    allowed.

## Raising a custom 403 from a dependency

To enforce a permission, resolve the identity first, then raise a 403 yourself.
[`HTTPException`](../reference.md#veloce.HTTPException) takes
`(status_code, detail, headers)`; use the constant from
[`status`](../reference.md) for clarity.

```python title="app.py"
from veloce import Depends, HTTPException, Security, TestClient, Veloce, status
from veloce.security import HTTPBearer

app = Veloce()
bearer = HTTPBearer()

# A real app would decode the token and load the user from a store.
_USERS = {"admin-token": {"name": "ada", "role": "admin"},
          "user-token": {"name": "ben", "role": "user"}}


async def current_user(token: str = Security(bearer)):
    user = _USERS.get(token)
    if user is None:
        # Authenticated channel, but the token is unknown -> still 401.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    return user


async def require_admin(user: dict = Depends(current_user)):
    if user["role"] != "admin":
        # Known caller, insufficient privilege -> 403.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


@app.get("/admin")
async def admin_area(user: dict = Depends(require_admin)):
    return {"hello": user["name"]}


client = TestClient(app)

assert client.get("/admin").status_code == 401  # no credential
assert client.get(
    "/admin", headers={"Authorization": "Bearer bad"}
).status_code == 401  # unknown token
assert client.get(
    "/admin", headers={"Authorization": "Bearer user-token"}
).status_code == 403  # known, but not admin
assert client.get(
    "/admin", headers={"Authorization": "Bearer admin-token"}
).status_code == 200
```

The scheme owns the 401 path; the 403 is a deliberate authorization choice in
`require_admin`. A handler may not silently downgrade a scheme's 401 — it only
adds a stricter check on top.

## Customising the 401 challenge

The schemes attach a `WWW-Authenticate` header on the 401. For Basic, Digest,
and the API-key schemes you can set a `realm` so clients show a meaningful
prompt; the value is quoted per [RFC 9110 §11.6.1](https://www.rfc-editor.org/rfc/rfc9110#section-11.6.1).

```python title="app.py"
from veloce import Security, TestClient, Veloce
from veloce.security import HTTPBasic

app = Veloce()
basic = HTTPBasic(realm="Staff area")


@app.get("/staff")
async def staff(creds=Security(basic)):
    return {"user": creds.username}


client = TestClient(app)

resp = client.get("/staff")
assert resp.status_code == 401
assert resp.headers["www-authenticate"] == 'Basic realm="Staff area"'
```

## Next steps

- Build a full login-to-bearer flow — see [Security schemes](../guide/security-schemes.md).
- Wire authorization through the resolver — see [Dependency injection](../guide/dependency-injection.md).
- Full signatures are in the [API reference](../reference.md).
