"""Tests for iteration-4: config, status module, jsonable_encoder, Security,
register_error_handler, routes property, logger."""

import datetime
import enum
import uuid
from decimal import Decimal
from typing import Any

import orjson
import pytest
from pydantic import BaseModel

from tests.conftest import make_request
from veloce import (
    Depends,
    HTTPBearer,
    JSONResponse,
    OAuth2PasswordBearer,
    Request,
    Security,
    Veloce,
    jsonable_encoder,
    status,
)

# ═══════════════════════════════════════════════════════════════
# status module
# ═══════════════════════════════════════════════════════════════


class TestStatusModule:
    def test_common_status_codes(self):
        assert status.HTTP_200_OK == 200
        assert status.HTTP_201_CREATED == 201
        assert status.HTTP_204_NO_CONTENT == 204
        assert status.HTTP_301_MOVED_PERMANENTLY == 301
        assert status.HTTP_400_BAD_REQUEST == 400
        assert status.HTTP_401_UNAUTHORIZED == 401
        assert status.HTTP_403_FORBIDDEN == 403
        assert status.HTTP_404_NOT_FOUND == 404
        assert status.HTTP_405_METHOD_NOT_ALLOWED == 405
        assert status.HTTP_422_UNPROCESSABLE_ENTITY == 422
        assert status.HTTP_429_TOO_MANY_REQUESTS == 429
        assert status.HTTP_500_INTERNAL_SERVER_ERROR == 500

    @pytest.mark.asyncio
    async def test_status_code_in_route(self):
        app = Veloce(openapi_url=None)

        @app.post("/items", status_code=status.HTTP_201_CREATED)
        async def create(request: Request):
            return {"id": 1}

        resp = await app.handle_request(make_request(method="POST", path="/items"))
        assert resp.status_code == 201


# ═══════════════════════════════════════════════════════════════
# jsonable_encoder
# ═══════════════════════════════════════════════════════════════


class TestJsonableEncoder:
    def test_pydantic_model(self):
        class User(BaseModel):
            name: str
            email: str
            password: str

        user = User(name="Alice", email="alice@example.com", password="secret")
        result = jsonable_encoder(user, exclude={"password"})
        assert result["name"] == "Alice"
        assert "password" not in result

    def test_dict(self):
        result = jsonable_encoder({"a": 1, "b": 2}, exclude={"b"})
        assert result == {"a": 1}

    def test_exclude_recurses_into_nested_dicts(self):
        """`exclude` strips matching keys at every depth — not only the
        top level. Catches a regression where nested calls dropped the
        filter and let `password` leak through inner dicts."""
        payload = {
            "user": {"name": "alice", "password": "p1"},
            "audit": [{"actor": "alice", "password": "p2"}],
            "password": "p0",
        }
        result = jsonable_encoder(payload, exclude={"password"})
        assert result == {"user": {"name": "alice"}, "audit": [{"actor": "alice"}]}

    def test_include_recurses_into_nested_dicts(self):
        """`include` keeps the same keys at every depth too."""
        payload = {"a": 1, "b": {"a": 2, "c": 3}, "d": 4}
        result = jsonable_encoder(payload, include={"a", "b"})
        # Top level keeps a and b; the nested dict under b also keeps
        # only the keys named in `include` (a). c at the inner level is
        # dropped because it is not in the include set.
        assert result == {"a": 1, "b": {"a": 2}}

    def test_datetime(self):
        dt = datetime.datetime(2024, 1, 15, 12, 30, 0)
        result = jsonable_encoder(dt)
        assert "2024-01-15" in result

    def test_uuid(self):
        u = uuid.UUID("12345678-1234-5678-1234-567812345678")
        result = jsonable_encoder(u)
        assert result == "12345678-1234-5678-1234-567812345678"

    def test_decimal(self):
        result = jsonable_encoder(Decimal("9.99"))
        assert result == 9.99

    def test_enum(self):
        class Color(str, enum.Enum):
            RED = "red"
            GREEN = "green"

        result = jsonable_encoder(Color.RED)
        assert result == "red"

    def test_set(self):
        result = jsonable_encoder({3, 1, 2})
        assert sorted(result) == [1, 2, 3]

    def test_nested(self):
        class Item(BaseModel):
            name: str
            created: datetime.datetime

        item = Item(name="Widget", created=datetime.datetime(2024, 1, 1))
        result = jsonable_encoder(item)
        assert result["name"] == "Widget"
        assert "2024-01-01" in result["created"]

    def test_list_of_models(self):
        class Item(BaseModel):
            name: str

        items = [Item(name="A"), Item(name="B")]
        result = jsonable_encoder(items)
        assert len(result) == 2
        assert result[0]["name"] == "A"

    def test_include(self):
        result = jsonable_encoder({"a": 1, "b": 2, "c": 3}, include={"a", "b"})
        assert result == {"a": 1, "b": 2}

    def test_pydantic_exclude_unset(self):
        class Item(BaseModel):
            name: str
            description: str | None = None
            price: float = 0.0

        item = Item(name="Widget")
        result = jsonable_encoder(item, exclude_unset=True)
        assert "name" in result
        assert "description" not in result

    def test_exclude_none_plain_dict(self):
        """`exclude_none` drops None-valued keys from a plain dict, not
        only from a BaseModel."""
        result = jsonable_encoder({"a": None, "b": 1}, exclude_none=True)
        assert result == {"b": 1}

    def test_exclude_none_recurses_into_nested_dicts(self):
        """`exclude_none` applies at every depth of a plain structure."""
        payload = {
            "a": None,
            "b": {"c": None, "d": 2},
            "e": [{"f": None, "g": 3}],
        }
        result = jsonable_encoder(payload, exclude_none=True)
        assert result == {"b": {"d": 2}, "e": [{"g": 3}]}

    def test_exclude_none_nested_dict_field_in_model(self):
        """A model field that is a plain dict has `exclude_none` applied
        during re-encoding, not just the model's own scalar fields."""

        class Wrapper(BaseModel):
            top: str | None = None
            meta: dict[str, Any]

        wrapper = Wrapper(top=None, meta={"x": None, "y": 1})
        result = jsonable_encoder(wrapper, exclude_none=True)
        assert "top" not in result
        assert result["meta"] == {"y": 1}

    def test_exclude_none_off_keeps_none(self):
        """Default behaviour is unchanged: None values are preserved."""
        result = jsonable_encoder({"a": None, "b": 1})
        assert result == {"a": None, "b": 1}


# ═══════════════════════════════════════════════════════════════
# Security dependency
# ═══════════════════════════════════════════════════════════════


class TestSecurityDependency:
    @pytest.mark.asyncio
    async def test_security_with_scopes(self):
        app = Veloce(openapi_url=None)
        oauth2 = OAuth2PasswordBearer(token_url="/token")

        @app.get("/users/me")
        async def me(token=Security(oauth2, scopes=["users:read"])):
            return {"token": token}

        resp = await app.handle_request(
            make_request(path="/users/me", headers={"authorization": "Bearer mytoken"})
        )
        assert resp.status_code == 200
        data = orjson.loads(resp.body)
        assert data["token"] == "mytoken"

    @pytest.mark.asyncio
    async def test_security_inherits_depends(self):
        # Security is a subclass of Depends
        security = HTTPBearer()
        dep = Security(security, scopes=["admin"])
        assert isinstance(dep, Depends)
        assert dep.scopes == ["admin"]


# ═══════════════════════════════════════════════════════════════
# app.config
# ═══════════════════════════════════════════════════════════════


class TestAppConfig:
    def test_config_dict(self):
        app = Veloce(openapi_url=None)
        app.config["DATABASE_URL"] = "postgres://localhost/db"
        app.config["DEBUG"] = True
        assert app.config["DATABASE_URL"] == "postgres://localhost/db"

    def test_config_update(self):
        app = Veloce(openapi_url=None)
        app.config.update(
            SECRET_KEY="my-secret",
            MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        )
        assert app.config["SECRET_KEY"] == "my-secret"

    def test_secret_key(self):
        app = Veloce(openapi_url=None)
        app.secret_key = "super-secret"
        assert app.secret_key == "super-secret"


# ═══════════════════════════════════════════════════════════════
# register_error_handler (non-decorator form)
# ═══════════════════════════════════════════════════════════════


class TestRegisterErrorHandler:
    @pytest.mark.asyncio
    async def test_register_by_status_code(self):
        app = Veloce(openapi_url=None)

        async def handle_404(request, exc):
            return JSONResponse({"custom": "not found"}, status_code=404)

        app.register_error_handler(404, handle_404)

        resp = await app.handle_request(make_request(path="/nonexistent"))
        assert resp.status_code == 404
        assert b"custom" in resp.body

    @pytest.mark.asyncio
    async def test_register_by_exception_class(self):
        app = Veloce(openapi_url=None)

        class CustomError(Exception):
            pass

        async def handle_custom(request, exc):
            return JSONResponse({"error": "custom"}, status_code=500)

        app.register_error_handler(CustomError, handle_custom)
        app._exception_handlers[CustomError] = handle_custom

        @app.get("/fail")
        async def fail(request: Request):
            raise CustomError("boom")

        resp = await app.handle_request(make_request(path="/fail"))
        assert b"custom" in resp.body


# ═══════════════════════════════════════════════════════════════
# app.routes property
# ═══════════════════════════════════════════════════════════════


class TestRoutesProperty:
    def test_routes_listing(self):
        app = Veloce(openapi_url=None)

        @app.get("/users", tags=["users"])
        async def list_users(request: Request):
            return []

        @app.post("/users", tags=["users"])
        async def create_user(request: Request):
            return {}

        routes = app.routes
        assert len(routes) >= 2
        paths = [r["path"] for r in routes]
        assert "/users" in paths
        methods = [r["method"] for r in routes]
        assert "GET" in methods
        assert "POST" in methods


# ═══════════════════════════════════════════════════════════════
# app.logger
# ═══════════════════════════════════════════════════════════════


class TestAppLogger:
    def test_logger_exists(self):
        # the logger name is the app's `import_name`.
        app = Veloce(import_name="my_api_pkg", openapi_url=None)
        assert app.logger is not None
        assert app.logger.name == "my_api_pkg"


# ═══════════════════════════════════════════════════════════════
# app.extensions
# ═══════════════════════════════════════════════════════════════


class TestExtensions:
    def test_extensions_registry(self):
        app = Veloce(openapi_url=None)
        app.extensions["cache"] = {"type": "redis", "url": "redis://localhost"}
        assert app.extensions["cache"]["type"] == "redis"
