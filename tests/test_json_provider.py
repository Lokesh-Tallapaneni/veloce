"""app.json + app.json_provider_class — the JSON provider."""

from __future__ import annotations

from veloce import Veloce
from veloce.json_provider import DefaultJSONProvider, JSONProvider


def test_default_provider_is_orjson_backed():
    app = Veloce(openapi_url=None)
    assert isinstance(app.json, DefaultJSONProvider)


def test_provider_dumps_loads_round_trip():
    app = Veloce(openapi_url=None)
    body = app.json.dumps({"a": 1, "b": [2, 3]})
    assert isinstance(body, bytes)
    assert app.json.loads(body) == {"a": 1, "b": [2, 3]}


def test_sort_keys_config_flag_threaded_through():
    app = Veloce(openapi_url=None)
    app.config["JSON_SORT_KEYS"] = True
    body = app.json.dumps({"b": 2, "a": 1})
    # Sorted output puts `a` first.
    assert body == b'{"a":1,"b":2}'


def test_indent_config_flag_threaded_through():
    app = Veloce(openapi_url=None)
    app.config["JSONIFY_PRETTYPRINT_REGULAR"] = True
    body = app.json.dumps({"a": 1})
    # Indented output spans multiple lines.
    assert b"\n" in body


def test_json_provider_class_swap():
    """Custom subclass picks up the override on next access."""
    captured: list[Any] = []  # type: ignore[name-defined]

    class TaggedProvider(DefaultJSONProvider):
        def dumps(self, obj, **kwargs):
            captured.append(obj)
            return super().dumps(obj, **kwargs)

    app = Veloce(openapi_url=None)
    app.json_provider_class = TaggedProvider
    # Force re-instantiation: clearing `_json_provider` is how the
    # property notices the class change.
    app._json_provider = None
    assert isinstance(app.json, TaggedProvider)
    app.json.dumps({"x": 1})
    assert captured == [{"x": 1}]


def test_json_setter_replaces_instance():
    app = Veloce(openapi_url=None)
    sentinel = DefaultJSONProvider(app)
    app.json = sentinel
    assert app.json is sentinel


def test_provider_response_builds_jsonresponse():
    from veloce.http.response import JSONResponse

    app = Veloce(openapi_url=None)
    resp = app.json.response({"hi": 1})
    assert isinstance(resp, JSONResponse)
    assert resp.body == b'{"hi":1}'


def test_subclassable_with_pure_dict_loads():
    """A custom provider can use a different parser (stdlib json)."""
    import json as stdlib_json

    class StdlibJSON(JSONProvider):
        def dumps(self, obj, **kwargs):
            return stdlib_json.dumps(obj).encode("utf-8")

        def loads(self, data):
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            return stdlib_json.loads(data)

    app = Veloce(openapi_url=None)
    app.json = StdlibJSON(app)
    body = app.json.dumps({"a": 1})
    assert app.json.loads(body) == {"a": 1}


def test_dumps_serialises_set_via_default_hook():
    app = Veloce(openapi_url=None)
    body = app.json.dumps({"ids": {3, 1, 2}})
    # Sets are emitted as a sorted list rather than raising TypeError.
    assert app.json.loads(body) == {"ids": [1, 2, 3]}


def test_dumps_serialises_path_decimal_and_bytes():
    import decimal
    from pathlib import PurePosixPath

    app = Veloce(openapi_url=None)
    body = app.json.dumps({"p": PurePosixPath("a/b"), "d": decimal.Decimal("1.5"), "b": b"hi"})
    assert app.json.loads(body) == {"p": "a/b", "d": 1.5, "b": "hi"}


def test_dumps_serialises_arbitrary_object_via_vars():
    class Point:
        def __init__(self) -> None:
            self.x = 1
            self.y = 2

    app = Veloce(openapi_url=None)
    body = app.json.dumps({"pt": Point()})
    assert app.json.loads(body) == {"pt": {"x": 1, "y": 2}}


def test_default_hook_recurses_into_nested_unsupported_leaves():
    import decimal

    class Wrap:
        def __init__(self) -> None:
            self.amount = decimal.Decimal("2.5")
            self.tags = {"b", "a"}

    app = Veloce(openapi_url=None)
    body = app.json.dumps(Wrap())
    # The custom object converts via vars(), then orjson re-enters the hook
    # for the nested Decimal and set members.
    assert app.json.loads(body) == {"amount": 2.5, "tags": ["a", "b"]}


def test_default_hook_applies_with_sort_keys_option():
    app = Veloce(openapi_url=None)
    body = app.json.dumps({"b": {2, 1}, "a": 0}, sort_keys=True)
    assert body == b'{"a":0,"b":[1,2]}'


def test_jsonresponse_serialises_set():
    from veloce.http.response import JSONResponse

    resp = JSONResponse({"vals": {1, 2}})
    assert resp.body == b'{"vals":[1,2]}'


def test_orjson_default_falls_back_to_str_for_slotted_object():
    from veloce.encoders import orjson_default

    # A slotted object has no __dict__, so vars() fails and the hook returns
    # str(obj) as a last resort - matching jsonable_encoder's behaviour.
    class Slotted:
        __slots__ = ()

    out = orjson_default(Slotted())
    assert isinstance(out, str)


def test_finite_decimal_encodes_as_json_number():
    import decimal

    from veloce.http.response import JSONResponse

    resp = JSONResponse({"price": decimal.Decimal("1.5")})
    assert resp.body == b'{"price":1.5}'


def test_out_of_float_range_decimal_encodes_as_string_not_null():
    import decimal

    from veloce.encoders import jsonable_encoder, orjson_default

    # float(Decimal('1E10000')) overflows to inf, which orjson would emit as
    # JSON null - silently dropping the value. The hook must preserve it as a
    # string instead. Both encode paths agree.
    big = decimal.Decimal("1E10000")
    assert orjson_default(big) == str(big)
    assert jsonable_encoder(big) == str(big)


def test_decimal_nan_encodes_as_string_not_null():
    import decimal

    from veloce.encoders import orjson_default

    nan = decimal.Decimal("NaN")
    assert orjson_default(nan) == str(nan)


def test_integer_valued_decimal_encodes_as_int():
    import decimal

    from veloce.encoders import jsonable_encoder

    out = jsonable_encoder(decimal.Decimal("1"))
    assert out == 1 and type(out) is int


def test_large_in_range_integer_decimal_keeps_exact_digits():
    import decimal

    from veloce.http.response import JSONResponse

    # < 2**64, so exact int round-trip with no e+19 precision loss.
    resp = JSONResponse({"v": decimal.Decimal("12345678901234567890")})
    assert resp.body == b'{"v":12345678901234567890}'


def test_huge_integer_decimal_falls_back_to_string():
    import decimal

    from veloce.encoders import jsonable_encoder, orjson_default
    from veloce.http.response import JSONResponse

    big = decimal.Decimal("1E10000")  # exponent 10000 >= 0, int out of 64-bit window
    assert orjson_default(big) == str(big)
    assert jsonable_encoder(big) == str(big)
    # And the full dump path does not raise orjson's 64-bit TypeError.
    assert b"1E+10000" in JSONResponse({"v": big}).body


def test_fractional_decimal_still_float():
    import decimal

    from veloce.encoders import jsonable_encoder

    assert jsonable_encoder(decimal.Decimal("9.99")) == 9.99
    out = jsonable_encoder(decimal.Decimal("1.0"))  # negative exponent -> float
    assert out == 1.0 and type(out) is float


# Add `Any` import for the type-checker.
from typing import Any  # noqa: E402
