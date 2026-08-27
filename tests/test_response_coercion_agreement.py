"""One value, one response, whichever coercer produced it.

Two public `make_response` entry points and the dispatch path gave three
different answers for the same value. `app.make_response` raised `TypeError` on
`123` and `None` while a handler returning them was answered `200` with a JSON
body, and `veloce.make_response((b"raw", 201))` JSON-encoded the whole tuple -
base64'd bytes and status code together - into a `200` body while
`app.make_response` honoured the status.

A user moving between the two silently got a different response. Dispatch keeps
its own fast lanes for the shapes a handler returns most, but all three now
answer alike.
"""

from __future__ import annotations

import orjson
import pytest

from veloce import Veloce, make_response
from veloce.testclient import TestClient

_VALUES = [
    ({"a": 1}, "dict"),
    ("text", "str"),
    (b"raw", "bytes"),
    (123, "int"),
    (None, "none"),
    ([1, 2], "list"),
    ((b"raw", 201), "tuple-status"),
    (("body", 202, {"X-Foo": "bar"}), "tuple-status-headers"),
]


def _through_dispatch(value):
    app = Veloce(openapi_url=None)

    @app.get("/r")
    async def handler():
        return value

    return TestClient(app).get("/r")


@pytest.mark.parametrize("value", [pytest.param(v, id=label) for v, label in _VALUES])
def test_all_three_coercers_agree_on_status(value):
    app = Veloce(openapi_url=None)
    dispatched = _through_dispatch(value)
    assert dispatched.status_code == make_response(value).status_code
    assert dispatched.status_code == app.make_response(value).status_code


@pytest.mark.parametrize("value", [pytest.param(v, id=label) for v, label in _VALUES])
def test_all_three_coercers_agree_on_body(value):
    app = Veloce(openapi_url=None)
    dispatched = _through_dispatch(value)
    assert dispatched.body == make_response(value).body
    assert dispatched.body == app.make_response(value).body


# ── The two rows that actually diverged ──────────────────────────────


@pytest.mark.parametrize("value", [123, None])
def test_a_scalar_is_coerced_rather_than_refused(value):
    """The defect: the public coercer raised where a handler return did not."""
    app = Veloce(openapi_url=None)
    response = app.make_response(value)
    assert response.status_code == 200
    assert orjson.loads(response.body) == value


def test_a_status_tuple_is_honoured_by_the_module_level_helper():
    """The defect: it JSON-encoded the tuple, status code and all, into a 200."""
    response = make_response((b"raw", 201))
    assert response.status_code == 201
    assert response.body == b"raw"


def test_a_status_and_headers_tuple_is_honoured_too():
    response = make_response(("body", 202, {"X-Foo": "bar"}))
    assert response.status_code == 202
    assert response.headers["X-Foo"] == "bar"
    assert response.body == b"body"


def test_a_headers_only_tuple_is_honoured():
    response = make_response(("body", {"X-Foo": "bar"}))
    assert response.status_code == 200
    assert response.headers["X-Foo"] == "bar"


def test_an_explicit_status_still_applies_without_a_tuple():
    """The existing two-argument form is unchanged."""
    assert make_response("hi", 418).status_code == 418
