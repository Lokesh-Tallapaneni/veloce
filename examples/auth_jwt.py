"""JWT login and a protected route.

Shows token-based authentication end to end with no external dependency:
a ``/login`` endpoint verifies a password and signs a JWT with ``encode_jwt``,
and a protected ``/me`` route extracts the Bearer token with ``HTTPBearer``,
verifies it with ``decode_jwt``, and returns the caller's claims.

Passwords are stored as PBKDF2 hashes via ``hash_password`` and checked with
``verify_password`` — never store plaintext.

Run it::

    python examples/auth_jwt.py

Then try::

    TOKEN=$(curl -s localhost:8000/login -d '{"username":"alice","password":"wonderland"}' \
        -H "Content-Type: application/json" | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
    curl localhost:8000/me -H "Authorization: Bearer $TOKEN"
"""

from __future__ import annotations

import time

from pydantic import BaseModel

from veloce import (
    Depends,
    HTTPBearer,
    HTTPException,
    JWTError,
    Veloce,
    decode_jwt,
    encode_jwt,
    hash_password,
    verify_password,
)

app = Veloce(title="JWT Auth", version="1.0.0")

# In production, read this from the environment, never hard-code it.
JWT_SECRET = "change-me-in-production"
TOKEN_TTL = 3600  # seconds

bearer = HTTPBearer()

# A fake user table keyed by username, storing a password *hash*.
_users: dict[str, dict] = {
    "alice": {"id": 1, "password_hash": hash_password("wonderland")},
}


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/login")
async def login(body: LoginRequest):
    user = _users.get(body.username)
    if user is None or not verify_password(user["password_hash"], body.password):
        raise HTTPException(401, "Incorrect username or password")
    now = int(time.time())
    token = encode_jwt(
        {"sub": body.username, "uid": user["id"], "iat": now, "exp": now + TOKEN_TTL},
        JWT_SECRET,
    )
    return {"access_token": token, "token_type": "bearer"}


def get_current_user(token: str = Depends(bearer)) -> dict:
    try:
        claims = decode_jwt(token, JWT_SECRET, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(401, "Invalid or expired token") from exc
    return {"username": claims["sub"], "id": claims["uid"]}


@app.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return user


if __name__ == "__main__":
    app.run(port=8000)
