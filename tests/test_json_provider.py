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


# Add `Any` import for the type-checker.
from typing import Any  # noqa: E402
