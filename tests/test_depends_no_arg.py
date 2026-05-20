"""Depends() with no argument infers the dependency from the annotation."""

from __future__ import annotations

from veloce import Depends, Request, Veloce
from veloce.testclient import TestClient


class Database:
    """A class-based dependency — its instance is injected."""

    def __init__(self) -> None:
        self.name = "primary"


class Settings:
    def __init__(self) -> None:
        self.env = "test"


def test_bare_depends_infers_class_from_annotation():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(request: Request, db: Database = Depends()):
        return {"db": db.name}

    with TestClient(app) as client:
        assert client.get("/x").json() == {"db": "primary"}


def test_bare_depends_returns_fresh_instance_type():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(request: Request, db: Database = Depends()):
        return {"is_database": isinstance(db, Database)}

    with TestClient(app) as client:
        assert client.get("/x").json() == {"is_database": True}


def test_explicit_depends_still_works():
    app = Veloce(openapi_url=None)

    def _get_token() -> str:
        return "tok"

    @app.get("/x")
    async def x(request: Request, token: str = Depends(_get_token)):
        return {"token": token}

    with TestClient(app) as client:
        assert client.get("/x").json() == {"token": "tok"}


def test_multiple_bare_depends():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(
        request: Request,
        db: Database = Depends(),
        settings: Settings = Depends(),
    ):
        return {"db": db.name, "env": settings.env}

    with TestClient(app) as client:
        assert client.get("/x").json() == {"db": "primary", "env": "test"}
