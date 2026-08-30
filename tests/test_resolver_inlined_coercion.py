"""The compiled resolver inlines coercion instead of calling through to it.

The generated resolver inlined the *slot* dispatch but not the *coercion*: every
parameter, on every request, still went through the generic helper with its type
as an argument.

    k['q'] = _cv(_qp['q'], _t0, 'q', 'query')

`_coerce_value` then re-answered a question fixed at registration - what type is
this? - by walking a chain of `is` comparisons. For `str`, the commonest
parameter type there is, the whole call did nothing but return its argument:

    _coerce_value('abc', str, ...)   93 ns    (measured)

so the generated code now reads the value straight into the slot and the call is
gone. `int` and `float` are emitted as the conversion itself, guarded so a bad
value still raises the same `RequestValidationError` the helper raised.

Everything else - `bool`, enums, `Literal`, and any type Pydantic has to handle -
keeps calling the helper. The point is to remove a call whose answer was already
known, not to reimplement coercion in generated source.

Note this is emitted code, so the risk it carries is that the *generated program*
diverges from the interpreter for some input. The parity tests below are the
important ones: they run the same parameter through both paths and require the
answers - values and error payloads alike - to be identical.
"""

from __future__ import annotations

import ast
import enum
import linecache
from typing import Literal

import pytest

from veloce import Depends, Header, Path, Query, Veloce
from veloce._handler_plan import build_plan
from veloce._resolver_codegen import compile_param_resolver
from veloce.dependency import _coerce_value
from veloce.exceptions import RequestValidationError
from veloce.testclient import TestClient


def _compile(handler):
    return compile_param_resolver(build_plan(handler), _coerce_value, RequestValidationError)


def _source(handler) -> str:
    resolver = _compile(handler)
    assert resolver is not None, "handler did not compile"
    return "".join(linecache.getlines(resolver.__code__.co_filename))


def _calls(handler, name: str) -> int:
    """How many times the generated resolver calls `name(...)` directly.

    `"int(" in source` is an unanchored substring: it is satisfied by any
    identifier ending in `int`, so a regression from the inlined conversion to
    a helper named `coerce_int(` or `_to_int(` would keep the assertion green
    while removing the thing it exists to prove. Parsing counts the call by its
    callee, not by its spelling.
    """
    return sum(
        1
        for node in ast.walk(ast.parse(_source(handler)))
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == name
    )


class Colour(enum.Enum):
    RED = "red"
    BLUE = "blue"


# ── what the generated source should look like ───────────────────────


def test_a_str_parameter_emits_no_coercion_call():
    """The defect: a call whose entire body was `return value`."""

    async def h(q: str = "d"):
        return q

    assert "_cv(" not in _source(h)


def test_an_unannotated_parameter_emits_no_coercion_call():
    """No annotation means `str`, which is the identity case."""

    async def h(q="d"):
        return q

    assert "_cv(" not in _source(h)


def test_an_int_parameter_emits_the_conversion_itself():
    async def h(q: int = 0):
        return q

    assert _calls(h, "int") >= 1


def test_a_float_parameter_emits_the_conversion_itself():
    async def h(q: float = 0.0):
        return q

    assert "float(" in _source(h)


def test_an_enum_parameter_still_calls_the_helper():
    """Not every type is worth inlining; the rest must keep working."""

    async def h(q: Colour = Colour.RED):
        return q

    assert "_cv(" in _source(h)


def test_a_bool_parameter_still_calls_the_helper():
    async def h(q: bool = False):
        return q

    assert "_cv(" in _source(h)


def test_a_mixed_handler_inlines_only_what_it_can():
    async def h(name: str = "x", age: int = 0, shade: Colour = Colour.RED):
        return name

    source = _source(h)
    assert _calls(h, "int") >= 1, "the int conversion is no longer inlined"
    assert "_cv(" in source, "the enum still goes through the converter"


# ── positive: values arrive correctly ────────────────────────────────


def _app():
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s(q: str = "default") -> dict:
        return {"q": q}

    @app.get("/i")
    async def i(n: int = 0) -> dict:
        return {"n": n}

    @app.get("/f")
    async def f(x: float = 0.0) -> dict:
        return {"x": x}

    @app.get("/p/{item}")
    async def p(item: str) -> dict:
        return {"item": item}

    @app.get("/pi/{item}")
    async def pi(item: int) -> dict:
        return {"item": item}

    @app.get("/opt")
    async def opt(q: str | None = None) -> dict:
        return {"q": q}

    return app


@pytest.mark.parametrize(
    "value",
    ["abc", "42", "3.14", "true", "false", "null", "", "  ", "ünïcødé", "a,b,c", "0"],
)
def test_a_str_query_value_arrives_verbatim(value: str):
    """A string that *looks* like another type must not be converted."""
    client = TestClient(_app())
    assert client.get("/s", params={"q": value}).json() == {"q": value}


def test_a_str_default_applies_when_absent():
    assert TestClient(_app()).get("/s").json() == {"q": "default"}


def test_an_optional_str_is_none_when_absent():
    assert TestClient(_app()).get("/opt").json() == {"q": None}


def test_an_optional_str_takes_a_value():
    assert TestClient(_app()).get("/opt", params={"q": "x"}).json() == {"q": "x"}


@pytest.mark.parametrize(("raw", "expected"), [("42", 42), ("-7", -7), ("0", 0), (" 8 ", 8)])
def test_an_int_query_value_coerces(raw: str, expected: int):
    assert TestClient(_app()).get("/i", params={"n": raw}).json() == {"n": expected}


@pytest.mark.parametrize(("raw", "expected"), [("3.5", 3.5), ("-0.25", -0.25), ("7", 7.0)])
def test_a_float_query_value_coerces(raw: str, expected: float):
    assert TestClient(_app()).get("/f", params={"x": raw}).json() == {"x": expected}


def test_a_str_path_value_arrives_verbatim():
    assert TestClient(_app()).get("/p/42").json() == {"item": "42"}


def test_an_int_path_value_coerces():
    assert TestClient(_app()).get("/pi/42").json() == {"item": 42}


# ── negative: a bad value still fails, and fails identically ─────────


def test_a_bad_int_is_rejected():
    response = TestClient(_app()).get("/i", params={"n": "abc"})
    assert response.status_code == 422


def test_a_bad_float_is_rejected():
    assert TestClient(_app()).get("/f", params={"x": "abc"}).status_code == 422


def test_a_bad_int_path_value_is_rejected():
    """An unconverted path segment is a validation failure, not a miss: the
    route matched, so the 422 names the offending parameter."""
    response = TestClient(_app()).get("/pi/abc")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["path", "item"]


def test_a_str_parameter_never_rejects():
    """Inlining identity must not accidentally introduce validation."""
    for value in ("abc", "", "42", "!!"):
        assert TestClient(_app()).get("/s", params={"q": value}).status_code == 200


# ── parity with the interpreter, which is the real guard ─────────────


def _parity_app():
    """Two routes per type: one compiled, one forced onto the interpreter."""

    def _dep():
        return 1

    app = Veloce(openapi_url=None)

    @app.get("/c_int")
    async def c_int(n: int = 0) -> dict:
        return {"n": n}

    @app.get("/i_int")
    async def i_int(n: int = 0, _d: int = Depends(_dep)) -> dict:
        return {"n": n}

    @app.get("/c_float")
    async def c_float(x: float = 0.0) -> dict:
        return {"x": x}

    @app.get("/i_float")
    async def i_float(x: float = 0.0, _d: int = Depends(_dep)) -> dict:
        return {"x": x}

    @app.get("/c_str")
    async def c_str(q: str = "d") -> dict:
        return {"q": q}

    @app.get("/i_str")
    async def i_str(q: str = "d", _d: int = Depends(_dep)) -> dict:
        return {"q": q}

    return app


def test_the_two_paths_are_actually_different_paths():
    """A parity test comparing one path with itself would prove nothing.

    The `/c_*` handlers compile; the `/i_*` handlers carry a `Depends`, which the
    param-only compiler rejects, so they run on the interpreter.
    """

    def _dep():
        return 1

    async def compiled(n: int = 0):
        return n

    async def interpreted(n: int = 0, _d: int = Depends(_dep)):
        return n

    assert _compile(compiled) is not None
    assert _compile(interpreted) is None


@pytest.mark.parametrize(("kind", "raw"), [("int", "abc"), ("float", "abc"), ("int", "")])
def test_a_bad_value_produces_the_same_422_on_both_paths(kind: str, raw: str):
    client = TestClient(_parity_app())
    field = "n" if kind == "int" else "x"
    compiled = client.get(f"/c_{kind}", params={field: raw})
    interpreted = client.get(f"/i_{kind}", params={field: raw})
    assert compiled.status_code == interpreted.status_code == 422
    assert compiled.json() == interpreted.json()


@pytest.mark.parametrize(("kind", "field", "raw"), [("int", "n", "42"), ("float", "x", "3.5")])
def test_a_good_value_produces_the_same_body_on_both_paths(kind: str, field: str, raw: str):
    client = TestClient(_parity_app())
    compiled = client.get(f"/c_{kind}", params={field: raw})
    interpreted = client.get(f"/i_{kind}", params={field: raw})
    assert compiled.json() == interpreted.json()


@pytest.mark.parametrize("raw", ["abc", "42", "", "ünïcødé"])
def test_a_str_value_is_identical_on_both_paths(raw: str):
    client = TestClient(_parity_app())
    assert (
        client.get("/c_str", params={"q": raw}).json()
        == client.get("/i_str", params={"q": raw}).json()
    )


# ── markers use the same emission ────────────────────────────────────


def test_a_str_query_marker_arrives_verbatim():
    app = Veloce(openapi_url=None)

    @app.get("/m")
    async def m(q: str = Query(default="d")) -> dict:
        return {"q": q}

    assert TestClient(app).get("/m", params={"q": "42"}).json() == {"q": "42"}


def test_an_int_query_marker_coerces_and_validates():
    app = Veloce(openapi_url=None)

    @app.get("/m")
    async def m(q: int = Query(gt=0)) -> dict:
        return {"q": q}

    client = TestClient(app)
    assert client.get("/m", params={"q": "5"}).json() == {"q": 5}
    assert client.get("/m", params={"q": "0"}).status_code == 422
    assert client.get("/m", params={"q": "abc"}).status_code == 422


def test_a_str_header_marker_arrives_verbatim():
    app = Veloce(openapi_url=None)

    @app.get("/m")
    async def m(token: str = Header(alias="x-token")) -> dict:
        return {"token": token}

    assert TestClient(app).get("/m", headers={"x-token": "42"}).json() == {"token": "42"}


def test_a_path_marker_still_validates():
    app = Veloce(openapi_url=None)

    @app.get("/m/{item_id}")
    async def m(item_id: int = Path(gt=0)) -> dict:
        return {"item_id": item_id}

    client = TestClient(app)
    assert client.get("/m/7").json() == {"item_id": 7}
    assert client.get("/m/0").status_code == 422


def test_a_list_of_str_collects_repeated_keys():
    app = Veloce(openapi_url=None)

    @app.get("/m")
    async def m(tags: list[str] = Query(default=[])) -> dict:
        return {"tags": tags}

    assert TestClient(app).get("/m?tags=a&tags=42").json() == {"tags": ["a", "42"]}


def test_a_list_of_int_coerces_each_element():
    app = Veloce(openapi_url=None)

    @app.get("/m")
    async def m(nums: list[int] = Query(default=[])) -> dict:
        return {"nums": nums}

    assert TestClient(app).get("/m?nums=1&nums=2").json() == {"nums": [1, 2]}


# ── types that keep the helper still behave ──────────────────────────


def test_an_enum_parameter_round_trips():
    app = Veloce(openapi_url=None)

    @app.get("/e")
    async def e(shade: Colour = Colour.RED) -> dict:
        return {"shade": shade.value}

    client = TestClient(app)
    assert client.get("/e", params={"shade": "blue"}).json() == {"shade": "blue"}
    assert client.get("/e", params={"shade": "green"}).status_code == 422


def test_a_bool_parameter_round_trips():
    app = Veloce(openapi_url=None)

    @app.get("/b")
    async def b(flag: bool = False) -> dict:
        return {"flag": flag}

    client = TestClient(app)
    assert client.get("/b", params={"flag": "true"}).json() == {"flag": True}
    assert client.get("/b", params={"flag": "0"}).json() == {"flag": False}


def test_a_literal_parameter_round_trips():
    app = Veloce(openapi_url=None)

    @app.get("/l")
    async def literal_route(mode: Literal["fast", "slow"] = "fast") -> dict:
        return {"mode": mode}

    client = TestClient(app)
    assert client.get("/l", params={"mode": "slow"}).json() == {"mode": "slow"}
    assert client.get("/l", params={"mode": "sideways"}).status_code == 422


# ── end to end: several parameters at once ───────────────────────────


def test_a_handler_with_every_inlined_kind_together():
    app = Veloce(openapi_url=None)

    @app.get("/all/{item_id}")
    async def all_kinds(item_id: int, name: str = "x", ratio: float = 1.0) -> dict:
        return {"item_id": item_id, "name": name, "ratio": ratio}

    body = TestClient(app).get("/all/9", params={"name": "42", "ratio": "0.5"}).json()
    assert body == {"item_id": 9, "name": "42", "ratio": 0.5}


def test_the_generated_resolver_does_not_drift_across_requests():
    """Generated code is built once and reused; a stateful bug would drift."""
    client = TestClient(_app())
    for _ in range(20):
        assert client.get("/s", params={"q": "abc"}).json() == {"q": "abc"}
        assert client.get("/i", params={"n": "7"}).json() == {"n": 7}
