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

## Upgrading hashes on login

Over time the recommended work factor rises, and you may switch from
PBKDF2 to scrypt. Rather than forcing a password reset, re-derive a
stored hash at the current strength the next time the user logs in —
that is the one moment the plaintext is available.
[`verify_and_needs_update`](../reference.md#veloce.verify_and_needs_update)
checks the password and reports whether the stored verifier is weaker
than the current defaults, returning `(ok, needs_update)`.

```python
from veloce import hash_password, verify_and_needs_update

# A legacy PBKDF2 verifier from an older deployment.
stored = hash_password("hunter2", method="pbkdf2:sha256")

ok, needs_update = verify_and_needs_update(stored, "hunter2")
if ok and needs_update:
    # Plaintext is in hand on a successful login — re-hash at the
    # current default (scrypt) and persist the new verifier.
    stored = hash_password("hunter2")
```

`needs_update` is `True` when the stored verifier used a non-default
method, or scrypt cost parameters below the current defaults. It is
always `False` on a failed verify — there is nothing to upgrade for a
credential that did not match.
[`needs_rehash`](../reference.md#veloce.needs_rehash) exposes the same
check without verifying, and
[`verify_and_needs_update_async`](../reference.md#veloce.verify_and_needs_update_async)
runs the verify on a worker thread for `async` handlers.

## Async handlers

The key derivation is deliberately slow — roughly 100 ms of CPU — so
calling `hash_password` or `verify_password` directly from an `async`
handler blocks the event loop for that whole time. From async code, use
[`hash_password_async`](../reference.md#veloce.hash_password_async) and
[`verify_password_async`](../reference.md#veloce.verify_password_async), which
run the KDF on a worker thread and leave the loop free for other requests.

```python
from veloce import (
    HTTPException,
    Request,
    Veloce,
    hash_password,
    hash_password_async,
    verify_password_async,
)

app = Veloce()

# A real app uses a database; a dict keeps the example self-contained.
_USERS: dict[str, str] = {}

# A throwaway hash computed once at startup. On the no-such-user path the
# submitted password is verified against this instead, which runs one real
# KDF so an unknown username costs the same as a wrong password.
_DUMMY_HASH = hash_password("password-that-no-account-uses")


@app.post("/signup")
async def signup(request: Request):
    body = await request.json()
    _USERS[body["username"]] = await hash_password_async(body["password"])
    return {"created": body["username"]}


@app.post("/login")
async def login(request: Request):
    body = await request.json()
    stored = _USERS.get(body["username"])
    # Fall back to the dummy hash when the user does not exist, so an unknown
    # username and a wrong password take the same time (one KDF either way).
    if not await verify_password_async(stored or _DUMMY_HASH, body["password"]):
        raise HTTPException(401, "Incorrect username or password")
    return {"ok": True}
```

The async variants accept the same arguments as their sync counterparts
and return the same values. Keep the sync `hash_password` /
`verify_password` for sync handlers, scripts, and CLI tools, where there
is no event loop to protect.

!!! tip "Defeating username enumeration"
    `verify_password` returns immediately — before any KDF — when the
    stored hash is missing or malformed, so a plain
    `stored is None or verify_password(...)` login leaks which usernames
    exist: an unknown account answers in microseconds while a wrong
    password takes ~100 ms. Compute one throwaway hash at startup with
    `hash_password(...)` and, on the no-such-user path, verify the submitted
    password against it (`verify_password(stored or _DUMMY_HASH, ...)`, as
    in the login handler above) so both cases pay exactly one KDF and cost
    the same.

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
