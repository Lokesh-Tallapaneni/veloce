"""Tests for veloce.security.api_key — slot discipline + construction."""

from __future__ import annotations

import pytest

from veloce import Depends, Veloce
from veloce.security.api_key import APIKeyCookie, APIKeyHeader, APIKeyQuery
from veloce.testclient import TestClient


class TestSlotDiscipline:
    """Each subclass must declare __slots__ = () so instances carry no __dict__."""

    def test_api_key_header_rejects_arbitrary_attribute(self):
        inst = APIKeyHeader(name="X-API-Key")
        with pytest.raises(AttributeError):
            inst.arbitrary_attr = 1

    def test_api_key_query_rejects_arbitrary_attribute(self):
        inst = APIKeyQuery(name="api_key")
        with pytest.raises(AttributeError):
            inst.arbitrary_attr = 1

    def test_api_key_cookie_rejects_arbitrary_attribute(self):
        inst = APIKeyCookie(name="session")
        with pytest.raises(AttributeError):
            inst.arbitrary_attr = 1

    @pytest.mark.parametrize("cls", [APIKeyHeader, APIKeyQuery, APIKeyCookie])
    def test_no_instance_dict(self, cls):
        inst = cls(name="X")
        assert not hasattr(inst, "__dict__")

    @pytest.mark.parametrize("cls", [APIKeyHeader, APIKeyQuery, APIKeyCookie])
    def test_realm_and_challenge_slots_carry_no_dict(self, cls):
        # The added `realm` / `_challenge` slots must not reintroduce a
        # __dict__ on any subclass.
        inst = cls(name="X", realm="admin")
        assert inst.realm == "admin"
        assert not hasattr(inst, "__dict__")
        with pytest.raises(AttributeError):
            inst.arbitrary_attr = 1


class TestConstruction:
    """The slotted attrs (name, auto_error) must still round-trip."""

    def test_constructs_with_defaults(self):
        inst = APIKeyHeader(name="X-API-Key")
        assert inst.name == "X-API-Key"
        assert inst.auto_error is True

    def test_constructs_with_auto_error_false(self):
        inst = APIKeyHeader(name="X-API-Key", auto_error=False)
        assert inst.name == "X-API-Key"
        assert inst.auto_error is False

    def test_query_and_cookie_construct(self):
        q = APIKeyQuery(name="api_key", auto_error=False)
        c = APIKeyCookie(name="session", auto_error=False)
        assert (q.name, q.auto_error) == ("api_key", False)
        assert (c.name, c.auto_error) == ("session", False)


class TestSourceAttr:
    """_source_attr is a class attribute — readable from class and instance."""

    def test_class_attribute_values(self):
        assert APIKeyHeader._source_attr == "headers"
        assert APIKeyQuery._source_attr == "query_params"
        assert APIKeyCookie._source_attr == "cookies"

    def test_readable_via_instance(self):
        assert APIKeyHeader(name="X")._source_attr == "headers"
        assert APIKeyQuery(name="X")._source_attr == "query_params"
        assert APIKeyCookie(name="X")._source_attr == "cookies"


class TestChallenge:
    """API-key 401s carry a `WWW-Authenticate: APIKey` challenge (RFC 9110 11.6.1)."""

    @staticmethod
    def _app(scheme):
        app = Veloce(openapi_url=None)

        @app.get("/protected")
        async def protected(key=Depends(scheme)):
            return {"key": key}

        return app

    def test_missing_key_returns_bare_apikey_challenge(self):
        app = self._app(APIKeyHeader(name="X-API-Key"))
        with TestClient(app) as client:
            resp = client.get("/protected")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "APIKey"

    def test_realm_appears_quoted_in_challenge(self):
        app = self._app(APIKeyHeader(name="X-API-Key", realm="admin"))
        with TestClient(app) as client:
            resp = client.get("/protected")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'APIKey realm="admin"'

    def test_double_quote_in_realm_is_backslash_escaped(self):
        # RFC 7230 Sec. 3.2.6 quoted-string escaping: `"` -> `\"`.
        app = self._app(APIKeyHeader(name="X-API-Key", realm='a"b'))
        with TestClient(app) as client:
            resp = client.get("/protected")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'APIKey realm="a\\"b"'

    def test_newline_in_realm_raises_at_construction(self):
        with pytest.raises(ValueError):
            APIKeyHeader(name="X-API-Key", realm="bad\nrealm")

    def test_auto_error_false_returns_none_without_401(self):
        app = self._app(APIKeyHeader(name="X-API-Key", auto_error=False))
        with TestClient(app) as client:
            resp = client.get("/protected")
        assert resp.status_code == 200
        assert resp.json() == {"key": None}
        assert "www-authenticate" not in resp.headers

    def test_valid_key_returns_200_with_key_value(self):
        app = self._app(APIKeyHeader(name="X-API-Key"))
        with TestClient(app) as client:
            resp = client.get("/protected", headers={"X-API-Key": "secret"})
        assert resp.status_code == 200
        assert resp.json() == {"key": "secret"}

    def test_query_scheme_emits_bare_apikey_challenge(self):
        app = self._app(APIKeyQuery(name="api_key"))
        with TestClient(app) as client:
            resp = client.get("/protected")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "APIKey"

    def test_cookie_scheme_emits_bare_apikey_challenge(self):
        app = self._app(APIKeyCookie(name="session"))
        with TestClient(app) as client:
            resp = client.get("/protected")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "APIKey"

    def test_subclass_can_override_challenge(self):
        class CustomHeader(APIKeyHeader):
            __slots__ = ()

            def challenge(self):
                return {"WWW-Authenticate": 'APIKey realm="custom", error="x"'}

        app = self._app(CustomHeader(name="X-API-Key"))
        with TestClient(app) as client:
            resp = client.get("/protected")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'APIKey realm="custom", error="x"'
