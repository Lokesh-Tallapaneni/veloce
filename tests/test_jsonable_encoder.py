"""jsonable_encoder — Pydantic/scalar/collection coercion to JSON-able types."""

from __future__ import annotations

import datetime
import enum
import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from veloce import jsonable_encoder
from veloce.encoders import orjson_default


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

    def test_scalar_re_pattern(self):
        import re

        pat = re.compile("ab")
        assert jsonable_encoder(pat) == "ab"
        assert orjson_default(pat) == "ab"

    def test_scalar_ipaddress(self):
        import ipaddress

        assert jsonable_encoder(ipaddress.IPv4Address("1.2.3.4")) == "1.2.3.4"
        assert jsonable_encoder(ipaddress.IPv6Address("::1")) == "::1"
        net = ipaddress.IPv4Network("1.2.3.0/24")
        assert jsonable_encoder(net) == "1.2.3.0/24"
        assert not isinstance(jsonable_encoder(net), dict)
        assert orjson_default(net) == "1.2.3.0/24"
        assert jsonable_encoder(ipaddress.IPv4Interface("1.2.3.4/24")) == "1.2.3.4/24"

    def test_deque_recurses(self):
        from collections import deque

        assert jsonable_encoder(deque([1, 2, 3])) == [1, 2, 3]
        assert jsonable_encoder({"d": deque([1, 2])}) == {"d": [1, 2]}
        u = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert jsonable_encoder(deque([u])) == [str(u)]
        assert orjson_default(deque([1, 2])) == [1, 2]

    def test_generator_drained(self):

        assert jsonable_encoder(x for x in [1, 2, 3]) == [1, 2, 3]
        assert orjson_default(x for x in [1, 2]) == [1, 2]

    def test_leaf_path_unaffected(self):
        assert jsonable_encoder(5) == 5
        assert jsonable_encoder("x") == "x"

    def test_vars_fallback_drops_private_attrs(self):

        class Plain:
            pass

        obj = Plain()
        obj.x = 1
        obj._sa_instance_state = object()
        obj._internal = "hidden"
        assert jsonable_encoder(obj) == {"x": 1}
        assert orjson_default(obj) == {"x": 1}

    def test_vars_fallback_opt_in_keeps_private(self):
        class Plain:
            __json_include_private__ = True

        obj = Plain()
        obj.x = 1
        obj._internal = "kept"
        result = jsonable_encoder(obj)
        assert result["x"] == 1
        assert result["_internal"] == "kept"

    def test_explicit_dict_with_underscore_keys_untouched(self):
        assert jsonable_encoder({"_x": 1, "y": 2}) == {"_x": 1, "y": 2}

    def test_slots_object_still_strs(self):
        class Slotted:
            __slots__ = ()

        result = jsonable_encoder(Slotted())
        assert isinstance(result, str)

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
