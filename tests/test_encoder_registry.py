"""Tests for jsonable_encoder MRO-walk dispatch, per-call custom_encoder, and
the process-level register_encoder registry."""

import base64
import datetime
from decimal import Decimal

import orjson
import pytest

from veloce import jsonable_encoder, register_encoder, unregister_encoder
from veloce.encoders import orjson_default

# ── MRO-walk dispatch for scalar subclasses ──


class MyInt(int):
    pass


class MyStr(str):
    pass


class MyFloat(float):
    pass


class MyDecimal(Decimal):
    pass


class TestMroDispatch:
    def test_int_subclass_encodes_as_int(self):
        assert jsonable_encoder(MyInt(5)) == 5
        assert isinstance(jsonable_encoder(MyInt(5)), int)

    def test_str_subclass_encodes_as_str(self):
        assert jsonable_encoder(MyStr("hi")) == "hi"

    def test_float_subclass_encodes_as_float(self):
        assert jsonable_encoder(MyFloat(1.5)) == 1.5

    def test_decimal_subclass_uses_decimal_encoder(self):
        # Integer-valued Decimal subclass keeps the int-preserving behaviour.
        assert jsonable_encoder(MyDecimal("1")) == 1
        assert jsonable_encoder(MyDecimal("1.5")) == 1.5

    def test_subclass_nested_in_container(self):
        assert jsonable_encoder({"n": MyInt(3), "items": [MyStr("a")]}) == {
            "n": 3,
            "items": ["a"],
        }

    def test_repeated_subclass_is_memoized(self):
        # Second call hits the memo cache; both must agree.
        assert jsonable_encoder(MyInt(1)) == 1
        assert jsonable_encoder(MyInt(2)) == 2


# ── Per-call custom_encoder ──


class Base:
    def __init__(self, v):
        self.v = v


class Derived(Base):
    pass


class TestCustomEncoder:
    def test_exact_type_match(self):
        out = jsonable_encoder(Base(1), custom_encoder={Base: lambda o: o.v})
        assert out == 1

    def test_exact_type_wins_over_isinstance(self):
        # Both Base and Derived match by isinstance, but the exact type(obj)
        # entry must win regardless of insertion order.
        d = Derived(7)
        ce = {Base: lambda o: "base", Derived: lambda o: "derived"}
        assert jsonable_encoder(d, custom_encoder=ce) == "derived"

    def test_insertion_order_tie_break(self):
        # No exact match for Derived; first isinstance hit in insertion order wins.
        d = Derived(0)
        first_wins = {Base: lambda o: "first", object: lambda o: "second"}
        assert jsonable_encoder(d, custom_encoder=first_wins) == "first"
        second_order = {object: lambda o: "second", Base: lambda o: "first"}
        assert jsonable_encoder(d, custom_encoder=second_order) == "second"

    def test_threaded_through_containers(self):
        ce = {Base: lambda o: o.v}
        assert jsonable_encoder({"a": [Base(1), Base(2)]}, custom_encoder=ce) == {"a": [1, 2]}

    def test_can_override_container_type(self):
        # custom_encoder runs before the container/leaf tables.
        out = jsonable_encoder({"k": 1}, custom_encoder={dict: lambda o: "DICT"})
        assert out == "DICT"

    def test_can_override_leaf_builtin(self):
        out = jsonable_encoder(
            datetime.datetime(2020, 1, 1),
            custom_encoder={datetime.datetime: lambda o: "fixed"},
        )
        assert out == "fixed"

    def test_no_custom_encoder_is_noop(self):
        assert jsonable_encoder({"k": 1}) == {"k": 1}

    def test_custom_encoder_cannot_bypass_secret_guard(self):
        from veloce.secret import Secret

        s = Secret("x")
        with pytest.raises(TypeError):
            jsonable_encoder(s, custom_encoder={Secret: lambda o: "leaked"})


# ── Process-level register_encoder ──


class Money:
    def __init__(self, cents):
        self.cents = cents


class TestRegisterEncoder:
    def teardown_method(self):
        unregister_encoder(Money)

    def test_register_and_encode(self):
        register_encoder(Money, lambda m: m.cents)
        assert jsonable_encoder(Money(150)) == 150

    def test_covers_subclasses_via_mro(self):
        class Subscription(Money):
            pass

        register_encoder(Money, lambda m: m.cents)
        assert jsonable_encoder(Subscription(99)) == 99

    def test_nested(self):
        register_encoder(Money, lambda m: m.cents)
        assert jsonable_encoder([Money(1), {"p": Money(2)}]) == [1, {"p": 2}]

    def test_unregister_restores_default(self):
        register_encoder(Money, lambda m: m.cents)
        assert jsonable_encoder(Money(5)) == 5
        unregister_encoder(Money)
        # Falls back to public-vars dict.
        assert jsonable_encoder(Money(5)) == {"cents": 5}

    def test_unregister_unknown_is_noop(self):
        unregister_encoder(Money)  # never registered in this state

    def test_overrides_builtin_handler(self):
        register_encoder(datetime.datetime, lambda d: "CUSTOM")
        try:
            assert jsonable_encoder(datetime.datetime(2020, 1, 1)) == "CUSTOM"
        finally:
            unregister_encoder(datetime.datetime)

    def test_works_on_orjson_default_path(self):
        register_encoder(Money, lambda m: m.cents)
        assert orjson_default(Money(7)) == 7
        assert orjson.dumps({"m": Money(8)}, default=orjson_default) == b'{"m":8}'

    def test_register_rejects_non_type(self):
        with pytest.raises(TypeError):
            register_encoder("notatype", lambda o: o)  # type: ignore[arg-type]

    def test_register_rejects_non_callable(self):
        with pytest.raises(TypeError):
            register_encoder(Money, "notcallable")  # type: ignore[arg-type]


# ── Default bytes for built-in types must be unchanged ──


class TestDefaultBytesUnchanged:
    def test_builtin_scalars_unchanged(self):
        assert jsonable_encoder(5) == 5
        assert jsonable_encoder("x") == "x"
        assert jsonable_encoder(1.5) == 1.5
        assert jsonable_encoder(True) is True
        assert jsonable_encoder(None) is None

    def test_decimal_int_vs_frac_unchanged(self):
        assert jsonable_encoder(Decimal("1")) == 1
        assert jsonable_encoder(Decimal("1.5")) == 1.5

    def test_datetime_unchanged(self):
        assert jsonable_encoder(datetime.datetime(2020, 1, 1, 12)) == "2020-01-01T12:00:00"

    def test_orjson_default_builtins_unchanged(self):
        assert (
            orjson.dumps({"d": datetime.datetime(2020, 1, 1)}, default=orjson_default)
            == b'{"d":"2020-01-01T00:00:00"}'
        )


# ── bytes / bytearray encode as lossless base64 ──


class TestBytesLosslessBase64:
    def test_jsonable_encoder_bytes_is_base64(self):
        assert jsonable_encoder(b"hi") == "aGk="

    def test_jsonable_encoder_bytearray_is_base64(self):
        assert jsonable_encoder(bytearray(b"hi")) == "aGk="

    def test_non_utf8_bytes_round_trip(self):
        # The original lossy decode mapped these to U+FFFD and lost the bytes;
        # base64 must reproduce the exact input on decode.
        raw = b"\xff\xfe\x00\x80"
        encoded = jsonable_encoder(raw)
        assert base64.b64decode(encoded) == raw

    def test_orjson_default_bytes_is_base64(self):
        assert orjson_default(b"\xff\xfe") == "//4="
        assert orjson.dumps({"b": b"\xff\xfe"}, default=orjson_default) == b'{"b":"//4="}'

    def test_orjson_default_bytearray_round_trip(self):
        raw = bytearray(b"\x00\x01\xff")
        assert base64.b64decode(orjson_default(raw)) == bytes(raw)


def test_orjson_default_resolves_scalar_subclass_without_registry():
    """The orjson default hook resolves a scalar subclass via its base (MRO walk)
    even with an empty registry, instead of falling through to `vars()` -> {}."""
    import datetime as _dt

    from veloce.encoders import orjson_default

    class _MyDateTime(_dt.datetime):
        pass

    class _MyFloat(float):
        pass

    dt = _MyDateTime(2024, 1, 2, 3, 4, 5)
    assert orjson_default(dt) == dt.isoformat()
    assert orjson_default(_MyFloat(1.5)) == 1.5
    # End-to-end through orjson with the default hook.
    import orjson

    assert orjson.dumps(_MyDateTime(2024, 1, 2), default=orjson_default) == b'"2024-01-02T00:00:00"'


def test_resolved_encoder_cache_is_bounded(monkeypatch):
    """The MRO-walk cache evicts (FIFO) past its cap so dynamically minted
    classes cannot grow it without bound."""
    import veloce.encoders as enc

    enc._RESOLVED_ENCODERS.clear()
    monkeypatch.setattr(enc, "_MAX_RESOLVED_ENCODERS", 4)
    for i in range(20):
        cls = type(f"_Dyn{i}", (int,), {})
        enc.jsonable_encoder(cls(i))
    assert len(enc._RESOLVED_ENCODERS) <= 4
    enc._RESOLVED_ENCODERS.clear()
