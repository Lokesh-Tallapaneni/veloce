"""OAuth2PasswordRequestFormStrict — grant_type is mandatory (SEC2)."""

from __future__ import annotations

from veloce import Depends, OAuth2PasswordRequestFormStrict, Veloce
from veloce.testclient import TestClient


def test_accepts_grant_type_password():
    app = Veloce(openapi_url=None)

    @app.post("/token")
    async def token(form: OAuth2PasswordRequestFormStrict = Depends()):
        return {"username": form.username, "grant_type": form.grant_type}

    with TestClient(app) as client:
        resp = client.post(
            "/token",
            data={"grant_type": "password", "username": "a", "password": "b"},
        )

    assert resp.json() == {"username": "a", "grant_type": "password"}


def test_missing_grant_type_is_422():
    app = Veloce(openapi_url=None)

    @app.post("/token")
    async def token(form: OAuth2PasswordRequestFormStrict = Depends()):
        return {}

    with TestClient(app) as client:
        resp = client.post("/token", data={"username": "a", "password": "b"})

    assert resp.status_code == 422


def test_wrong_grant_type_is_422():
    app = Veloce(openapi_url=None)

    @app.post("/token")
    async def token(form: OAuth2PasswordRequestFormStrict = Depends()):
        return {}

    with TestClient(app) as client:
        resp = client.post(
            "/token",
            data={"grant_type": "client_credentials", "username": "a", "password": "b"},
        )

    assert resp.status_code == 422


def test_strict_is_subclass_of_non_strict():
    from veloce import OAuth2PasswordRequestForm

    assert issubclass(OAuth2PasswordRequestFormStrict, OAuth2PasswordRequestForm)
