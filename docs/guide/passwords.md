---
description: Password hashing in Veloce — hash_password and verify_password using scrypt or PBKDF2, async variants for handlers, and is_strong_password policy checks.
tags: [security, passwords, hashing, scrypt, pbkdf2]
---

# Passwords

Veloce includes salted, slow password hashing built entirely on the
standard library. [`hash_password`](../reference.md#veloce.hash_password) turns
a plaintext password into a self-describing verifier string, and
[`verify_password`](../reference.md#veloce.verify_password) checks a candidate
against a stored verifier in constant time. Never store plaintext passwords;
store the output of `hash_password`.

```python
from veloce import hash_password, verify_password

stored = hash_password("correct horse battery staple")
assert verify_password(stored, "correct horse battery staple") is True
assert verify_password(stored, "wrong password") is False
```

The `stored` value is a single string of the form
`method$params$salt$hash`. Put that string straight into a database
column — it carries the algorithm, work factors, and salt needed to
verify later, so you never store those separately.

## Choosing a method

`hash_password` accepts a `method` argument. Two methods are supported,
both stdlib-only:

- `"scrypt"` (the default) — memory-hard ([RFC 7914](https://www.rfc-editor.org/rfc/rfc7914)),
  resistant to GPU and ASIC brute force.
- `"pbkdf2:sha256"` — CPU-only ([NIST SP 800-132](https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-132.pdf)),
  portable to restricted Python builds that lack scrypt.

```python
from veloce import hash_password, verify_password

stored = hash_password("hunter2", method="pbkdf2:sha256")
assert verify_password(stored, "hunter2") is True
```

Unless you have a specific portability constraint, keep the scrypt
default. Both methods produce the same `method$params$salt$hash` shape,
and `verify_password` reads the method tag from the stored string — you
do not pass `method` to `verify_password`.

The `salt_length` argument controls how many random bytes form the salt
(default 16, the OWASP minimum). Values below 8 raise `ValueError`.

## Verifying never raises

`verify_password` returns a `bool` and never raises. A malformed stored
string, an unknown method tag, an empty candidate, or a plain mismatch
all return `False`. This lets you treat the call as a single boolean
check without wrapping it in `try`.

```python
from veloce import verify_password

assert verify_password("not-a-valid-hash", "anything") is False
assert verify_password("", "anything") is False
```

!!! note "Tampered work factors are rejected"
    `verify_password` enforces minimum cost floors at verify time. A
    stored hash whose scrypt cost or PBKDF2 iteration count has been
    lowered below the security floor (for example to make verification
    trivially fast) is treated as tampering and returns `False`, even if
    its format is otherwise valid.

## Async handlers

The key derivation is deliberately slow — roughly 100 ms of CPU — so
calling `hash_password` or `verify_password` directly from an `async`
handler blocks the event loop for that whole time. From async code, use
[`hash_password_async`](../reference.md#veloce.hash_password_async) and
[`verify_password_async`](../reference.md#veloce.verify_password_async), which
run the KDF on a worker thread and leave the loop free for other requests.

```python
import secrets

from veloce import (
    HTTPException,
    Request,
    Veloce,
    hash_password_async,
    verify_password_async,
)

app = Veloce()

# A real app uses a database; a dict keeps the example self-contained.
_USERS: dict[str, str] = {}


@app.post("/signup")
async def signup(request: Request):
    body = await request.json()
    _USERS[body["username"]] = await hash_password_async(body["password"])
    return {"created": body["username"]}


@app.post("/login")
async def login(request: Request):
    body = await request.json()
    stored = _USERS.get(body["username"])
    if stored is None or not await verify_password_async(stored, body["password"]):
        raise HTTPException(401, "Incorrect username or password")
    return {"ok": True}
```

The async variants accept the same arguments as their sync counterparts
and return the same values. Keep the sync `hash_password` /
`verify_password` for sync handlers, scripts, and CLI tools, where there
is no event loop to protect.

!!! tip "Constant-time username comparison"
    `verify_password` already compares the derived hash in constant time.
    To avoid leaking which usernames exist through response timing,
    consider verifying against a dummy hash when the username is unknown,
    or compare with `secrets.compare_digest` (imported above) where a
    fixed-string comparison is needed.

## Password strength policy

[`is_strong_password`](../reference.md#veloce.is_strong_password) is a cheap
baseline policy check. It returns `True` only when the password is at least
`min_length` characters (default 8) and contains at least one letter and
at least one digit.

```python
from veloce import is_strong_password

assert is_strong_password("abc12345") is True
assert is_strong_password("short1") is False        # under 8 characters
assert is_strong_password("allletters") is False    # no digit
assert is_strong_password("123456789") is False     # no letter
```

Tighten the floor with the keyword-only `min_length` argument:

```python
from veloce import is_strong_password

assert is_strong_password("abc12345", min_length=12) is False
assert is_strong_password("abcdefgh1234", min_length=12) is True
```

!!! note "This is a baseline, not a full policy"
    `is_strong_password` is intentionally minimal. For
    [NIST SP 800-63B](https://pages.nist.gov/800-63-3/sp800-63b.html)
    style policy — blocking known-leaked passwords, dropping arbitrary
    composition rules, removing low maximum-length caps — layer your own
    checks on top. Reject weak passwords at signup before calling
    `hash_password`.

A signup endpoint usually combines the strength check with hashing:

```python
from veloce import (
    HTTPException,
    Request,
    Veloce,
    hash_password_async,
    is_strong_password,
)

app = Veloce()
_USERS: dict[str, str] = {}


@app.post("/signup")
async def signup(request: Request):
    body = await request.json()
    if not is_strong_password(body["password"]):
        raise HTTPException(422, "Password too weak")
    _USERS[body["username"]] = await hash_password_async(body["password"])
    return {"created": body["username"]}
```

## Next steps

- [Security schemes](security-schemes.md) — extract credentials from
  requests with HTTP Basic, Bearer, API key, and OAuth2 schemes.
- [Signing](signing.md) — issue signed, time-limited tokens for sessions
  and password-reset links.
- The [API reference](../reference.md) has full signatures for every
  hashing helper.

For background on the algorithms and storage advice, see the
[OWASP Password Storage cheat sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
