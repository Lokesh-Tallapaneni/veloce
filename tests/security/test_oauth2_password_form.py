"""OAuth2PasswordRequestForm usable as a Depends() class dependency (SEC2)."""

from __future__ import annotations

from veloce import Depends, Veloce
from veloce.security.oauth2 import OAuth2PasswordRequestForm
from veloce.testclient import TestClient


def test_reads_credentials_from_form_body():
    app = Veloce(openapi_url=None)

    @app.post("/token")
    async def token(form: OAuth2PasswordRequestForm = Depends()):
        return {"username": form.username, "password": form.password}

    with TestClient(app) as client:
        resp = client.post("/token", data={"username": "alice", "password": "s3cret"})

    assert resp.json() == {"username": "alice", "password": "s3cret"}


def test_grant_type_defaults_to_password():
    app = Veloce(openapi_url=None)

    @app.post("/token")
    async def token(form: OAuth2PasswordRequestForm = Depends()):
        return {"grant_type": form.grant_type}

    with TestClient(app) as client:
        resp = client.post("/token", data={"username": "a", "password": "b"})

    assert resp.json() == {"grant_type": "password"}


def test_optional_client_fields_present():
    app = Veloce(openapi_url=None)

    @app.post("/token")
    async def token(form: OAuth2PasswordRequestForm = Depends()):
        return {"client_id": form.client_id, "scope": form.scope}

    with TestClient(app) as client:
        resp = client.post(
            "/token",
            data={"username": "a", "password": "b", "client_id": "app1", "scope": "read"},
        )

    assert resp.json() == {"client_id": "app1", "scope": "read"}


def test_direct_construction_with_kwargs():
    form = OAuth2PasswordRequestForm(username="bob", password="pw")
    assert form.username == "bob"
    assert form.password == "pw"
    assert form.grant_type == "password"


def test_direct_no_arg_construction_uses_defaults():
    form = OAuth2PasswordRequestForm()
    assert form.username == ""
    assert form.client_id is None
    assert form.grant_type == "password"
