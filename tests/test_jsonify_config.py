"""jsonify config-driven options (Q32)."""

from __future__ import annotations

import orjson

from veloce import Request, Veloce, jsonify
from veloce.testclient import TestClient


def test_jsonify_keeps_insertion_order_by_default():
    """The built-in default: `JSON_SORT_KEYS` is False, so order is as written.

    Sorting costs 24-49% of the serialise and is not what most JSON APIs do, so
    it is opt-in. `test_jsonify_sort_keys_when_config_set` covers the opt-in.
    """
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return jsonify(b=2, a=1, c=3)

    resp = TestClient(app).get("/x")
    assert resp.body == b'{"b":2,"a":1,"c":3}'


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


def test_jsonify_kwargs():
    resp = jsonify(name="alice", age=30)
    assert resp.status_code == 200
    data = orjson.loads(resp.body)
    assert data["name"] == "alice"


def test_jsonify_dict():
    resp = jsonify({"x": 1})
    assert orjson.loads(resp.body) == {"x": 1}


def test_jsonify_list():
    resp = jsonify([1, 2, 3])
    assert orjson.loads(resp.body) == [1, 2, 3]


def test_jsonify_via_testclient():
    app = Veloce(openapi_url=None)

    @app.get("/j")
    async def j(request: Request):
        return jsonify({"x": 1})

    with TestClient(app) as client:
        resp = client.get("/j")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"x": 1}
