"""Tests for veloce.security.api_key — slot discipline + construction."""

from __future__ import annotations

import pytest

from veloce.security.api_key import APIKeyCookie, APIKeyHeader, APIKeyQuery


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

    def test_no_instance_dict(self):
        for cls in (APIKeyHeader, APIKeyQuery, APIKeyCookie):
            inst = cls(name="X")
            assert not hasattr(inst, "__dict__"), f"{cls.__name__} leaked __dict__"


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
