---
description: Signed value serialisation in Veloce — Signer.dumps / loads with HMAC-SHA256, timed signatures via max_age, per-purpose salts, and BadSignature handling.
tags: [security, signing, hmac, tokens]
---

# Signing

[`Signer`](../reference.md#veloce.Signer) turns a JSON-serialisable value into
a signed, timestamped, URL-safe token and verifies it later. Use it when
the server hands a value to the client, the client returns it, and the
server needs to know the value was not tampered with — password-reset
links, email-confirmation tokens, signed cookies, and similar. Signing
is built on HMAC-SHA256 ([RFC 2104](https://www.rfc-editor.org/rfc/rfc2104))
from the standard library.

```python
from veloce.signing import Signer

signer = Signer(secret="server-secret", salt="reset-token")

token = signer.dumps({"user_id": 42})
data = signer.loads(token)
assert data == {"user_id": 42}
```

!!! note "Import path"
    `Signer`, `BadSignature`, `BadTimeSignature`, and `BadData` are part
    of the top-level public API and can also be imported directly with
    `from veloce import Signer`. This guide imports from
    `veloce.signing` to keep the source module explicit.

A token is not encryption — the payload is signed, not hidden. Anyone can
read the value inside a token; they just cannot change it without the
secret. Do not put data in a token that the client must not see.

## Signing and reading values

[`Signer.dumps`](../reference.md#veloce.Signer.dumps) serialises the value to
JSON, appends a timestamp, and signs both.
[`Signer.loads`](../reference.md#veloce.Signer.loads) verifies the signature
and returns the original value. The signature is checked before the payload
is decoded, so a tampered token is rejected before its JSON ever reaches the
parser.

```python
from veloce.signing import Signer

signer = Signer(secret="server-secret")

token = signer.dumps({"email": "alice@example.com", "action": "confirm"})
claims = signer.loads(token)
assert claims["email"] == "alice@example.com"
```

The value passed to `dumps` may be any JSON-serialisable object — a dict,
a list, a string, a number. `loads` returns exactly what you put in.

## Per-purpose salts

The `salt` argument (default `"veloce.signing"`) derives a distinct
signing key from the same secret. Tokens signed with one salt do not
verify under another, so you can share one application secret across
purposes — sessions, CSRF, password reset — without those tokens
cross-validating.

```python
from veloce.signing import BadSignature, Signer

reset_signer = Signer(secret="app-secret", salt="password-reset")
confirm_signer = Signer(secret="app-secret", salt="email-confirm")

token = reset_signer.dumps({"user_id": 7})

# The same secret with a different salt cannot read the token.
try:
    confirm_signer.loads(token)
except BadSignature:
    print("reset token rejected by the confirm signer")
```

Use a different, descriptive salt per token purpose. The secret stays the
same; the salt keeps the namespaces apart.

## Timed signatures

Pass `max_age` (in seconds) to [`Signer.loads`](../reference.md#veloce.Signer.loads)
to reject tokens older than a window. This is what makes a password-reset
link expire.

```python
from veloce.signing import BadTimeSignature, Signer

signer = Signer(secret="server-secret", salt="password-reset")
token = signer.dumps({"user_id": 42})

# Within the window the token reads back normally.
assert signer.loads(token, max_age=3600) == {"user_id": 42}

# A token older than max_age raises BadTimeSignature. A freshly minted
# token with max_age=0 is already past its window.
try:
    signer.loads(token, max_age=0)
except BadTimeSignature:
    print("token expired")
```

The timestamp is embedded in the token at `dumps` time, so the same token
can be checked against different `max_age` windows by different callers.
A token whose timestamp is in the future (a clock skew or a forged date)
also raises `BadTimeSignature`.

## Handling failures

Verification raises one of three exceptions, all subclasses of
[`BadSignature`](../reference.md#veloce.BadSignature):

- [`BadSignature`](../reference.md#veloce.BadSignature) — the signature did not
  match the configured secret (tampering, wrong secret, or wrong salt).
- [`BadTimeSignature`](../reference.md#veloce.BadTimeSignature) — the signature
  verified but the token is older than `max_age` (or future-dated).
- [`BadData`](../reference.md#veloce.BadData) — the token was structurally
  malformed (bad base64, invalid JSON, or not a string).

Because `BadTimeSignature` and `BadData` both inherit from
`BadSignature`, a single `except BadSignature` catches all three when you
do not need to distinguish them.

```python
from veloce import HTTPException, Veloce
from veloce.signing import BadSignature, Signer

app = Veloce()
signer = Signer(secret="server-secret", salt="password-reset")


@app.get("/reset/{token}")
async def reset(token: str):
    try:
        claims = signer.loads(token, max_age=3600)
    except BadSignature:
        raise HTTPException(400, "Invalid or expired reset link")
    return {"user_id": claims["user_id"]}
```

Catching `BadSignature` here covers expired links, tampered tokens, and
malformed input with one branch — the user sees the same generic error
for all of them, which avoids leaking why a token was rejected.

## Rotating the secret

To change the signing secret without invalidating tokens already in the
wild, configure the new secret as primary and register the old one as a
fallback with [`add_fallback_secret`](../reference.md#veloce.Signer.add_fallback_secret).
New tokens are signed with the primary secret; tokens signed with the
fallback still verify during the rotation window.

```python
from veloce.signing import Signer

signer = Signer(secret="new-secret", salt="password-reset")
signer.add_fallback_secret("old-secret", salt="password-reset")

# Tokens minted under the old secret still verify; new tokens use the new one.
```

The fallback secret is accepted for verification only — it is never used
to sign new tokens. Remove the fallback once the rotation window has
passed and old tokens have expired.

## Next steps

- [Security schemes](security-schemes.md) — extract Bearer tokens and API
  keys from incoming requests.
- [Passwords](passwords.md) — hash and verify passwords for the login
  endpoint that issues your signed tokens.
- [Sessions](sessions.md) — Veloce's session support builds on the same
  signing primitives.
- The [API reference](../reference.md) has full signatures for `Signer`
  and the exception hierarchy.
