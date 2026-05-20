"""jsonify config-driven options (Q32)."""

from __future__ import annotations

from veloce import Veloce, jsonify
from veloce.testclient import TestClient


def test_jsonify_default_sorts_keys():
    """the built-in default: `JSON_SORT_KEYS` is True → keys sorted, no indent."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return jsonify(b=2, a=1, c=3)

    resp = TestClient(app).get("/x")
    # spec-faithful: keys sorted alphabetically, compact output.
    assert resp.body == b'{"a":1,"b":2,"c":3}'


def test_jsonify_no_sort_when_disabled():
    """`JSON_SORT_KEYS=False` keeps insertion order."""
    app = Veloce(debug=True, openapi_url=None)
    app.config["JSON_SORT_KEYS"] = False

    @app.get("/x")
    async def x():
        return jsonify(b=2, a=1, c=3)

    resp = TestClient(app).get("/x")
    assert resp.body == b'{"b":2,"a":1,"c":3}'


def test_jsonify_sort_keys_when_config_set():
    app = Veloce(debug=True, openapi_url=None)
    app.config["JSON_SORT_KEYS"] = True

    @app.get("/x")
    async def x():
        return jsonify(b=2, a=1, c=3)

    resp = TestClient(app).get("/x")
    assert resp.body == b'{"a":1,"b":2,"c":3}'


def test_jsonify_pretty_print_when_config_set():
    """JSONIFY_PRETTYPRINT_REGULAR=True → 2-space indented output."""
    app = Veloce(debug=True, openapi_url=None)
    app.config["JSONIFY_PRETTYPRINT_REGULAR"] = True

    @app.get("/x")
    async def x():
        return jsonify({"a": 1, "b": [1, 2]})

    resp = TestClient(app).get("/x")
    # Indented output contains newlines and leading whitespace.
    assert b"\n" in resp.body
    assert b"  " in resp.body
    # Still valid JSON.
    import orjson

    assert orjson.loads(resp.body) == {"a": 1, "b": [1, 2]}


def test_jsonify_both_flags_combined():
    app = Veloce(debug=True, openapi_url=None)
    app.config["JSON_SORT_KEYS"] = True
    app.config["JSONIFY_PRETTYPRINT_REGULAR"] = True

    @app.get("/x")
    async def x():
        return jsonify(z=1, a=2)

    resp = TestClient(app).get("/x")
    text = resp.body.decode()
    # `a` appears before `z` because of sort_keys.
    assert text.index('"a"') < text.index('"z"')
    # Pretty-printed → contains newlines.
    assert "\n" in text


def test_jsonify_outside_request_uses_defaults():
    """When called outside a request (no current_app), use plain defaults."""
    # This must not raise even though there's no app context.
    resp = jsonify(b=2, a=1)
    assert resp.body == b'{"b":2,"a":1}'


def test_jsonify_positional_dict_passthrough():
    """Calling `jsonify({...})` returns that dict as JSON."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return jsonify({"k": "v"})

    resp = TestClient(app).get("/x")
    assert resp.body == b'{"k":"v"}'


def test_jsonify_list_passthrough():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return jsonify([1, 2, 3])

    resp = TestClient(app).get("/x")
    assert resp.body == b"[1,2,3]"
