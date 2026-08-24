"""Which route answers must not depend on the order the decorators appear.

`/items/{id:int}` and `/items/{slug:str}` both match `/items/42`, and the more
restrictive one has to win or the generic route swallows everything. That
ordering came from a table of five known converter classes; the other six
built-ins, and every `register_converter` type, scored the same as `str`. So
`/a/{d:date}` and `/a/{slug:str}` were a coin toss decided by which line came
first in the file - and moving a decorator during an unrelated refactor changed
which handler ran.

The converter now declares how restrictive it is, next to what it matches.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.routing.converters import _Converter, register_converter
from veloce.testclient import TestClient


def _app(*specs: tuple[str, str]) -> Veloce:
    """Register `(path, label)` routes in the order given."""
    app = Veloce(openapi_url=None)
    for path, label in specs:

        def make(bound=label):
            async def handler(**kwargs):
                return {"matched": bound}

            return handler

        app.add_route(path, make(), methods=["GET"], name=f"r_{label}")
    return app


def _who(app: Veloce, path: str) -> str:
    return TestClient(app).get(path).json()["matched"]


# ── the defect ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("specific", "sample"),
    [
        ("date", "2020-01-01"),
        ("datetime", "2020-01-01T10:30:00"),
        ("time", "10:30:00"),
        ("uuid", "123e4567-e89b-12d3-a456-426614174000"),
        ("int", "42"),
        ("decimal", "1.5"),
        ("float", "1.5"),
    ],
)
def test_a_restrictive_converter_wins_whatever_the_order(specific, sample):
    """The defect: six of these depended on which route was declared first."""
    forward = _app((f"/a/{{v:{specific}}}", specific), ("/a/{slug:str}", "str"))
    reverse = _app(("/a/{slug:str}", "str"), (f"/a/{{v:{specific}}}", specific))
    assert _who(forward, f"/a/{sample}") == specific
    assert _who(reverse, f"/a/{sample}") == specific


def test_a_value_the_restrictive_converter_rejects_falls_through_to_str():
    """Specificity orders the attempts; it must not stop `str` matching."""
    for app in (
        _app(("/a/{d:date}", "date"), ("/a/{slug:str}", "str")),
        _app(("/a/{slug:str}", "str"), ("/a/{d:date}", "date")),
    ):
        assert _who(app, "/a/not-a-date") == "str"


def test_timedelta_beats_str_either_way():
    forward = _app(("/a/{v:timedelta}", "timedelta"), ("/a/{slug:str}", "str"))
    reverse = _app(("/a/{slug:str}", "str"), ("/a/{v:timedelta}", "timedelta"))
    assert _who(forward, "/a/PT1H") == "timedelta"
    assert _who(reverse, "/a/PT1H") == "timedelta"


def test_a_literal_choice_set_beats_str_either_way():
    forward = _app(("/a/{v:any(red,blue)}", "any"), ("/a/{slug:str}", "str"))
    reverse = _app(("/a/{slug:str}", "str"), ("/a/{v:any(red,blue)}", "any"))
    assert _who(forward, "/a/red") == "any"
    assert _who(reverse, "/a/red") == "any"
    assert _who(reverse, "/a/green") == "str"


# ── the ordering the table already got right, still right ────────────


def test_int_still_beats_str():
    assert _who(_app(("/a/{slug:str}", "str"), ("/a/{n:int}", "int")), "/a/42") == "int"


def test_uuid_beats_int_and_str():
    sample = "123e4567-e89b-12d3-a456-426614174000"
    app = _app(("/a/{slug:str}", "str"), ("/a/{n:int}", "int"), ("/a/{u:uuid}", "uuid"))
    assert _who(app, f"/a/{sample}") == "uuid"


def test_path_is_tried_last():
    """Greedy: it would swallow every sibling if it were tried first."""
    app = _app(("/a/{rest:path}", "path"), ("/a/{n:int}", "int"))
    assert _who(app, "/a/42") == "int"
    assert _who(app, "/a/deep/nested/thing") == "path"


def test_a_static_segment_still_beats_every_converter():
    app = Veloce(openapi_url=None)

    @app.get("/a/exact")
    async def exact():
        return {"matched": "static"}

    @app.get("/a/{n:int}")
    async def dynamic(n: int):
        return {"matched": "int"}

    client = TestClient(app)
    assert client.get("/a/exact").json()["matched"] == "static"
    assert client.get("/a/7").json()["matched"] == "int"


# ── custom converters ────────────────────────────────────────────────


def test_a_custom_converter_can_declare_itself_more_specific_than_str():
    """The point of moving the value onto the converter."""

    class SlugConverter(_Converter):
        __slots__ = ()
        specificity = 25

        def match(self, value):
            ok = value.replace("-", "").isalnum() and "-" in value
            return ok, value

    register_converter("slug25", SlugConverter)
    forward = _app(("/a/{v:slug25}", "slug"), ("/a/{s:str}", "str"))
    reverse = _app(("/a/{s:str}", "str"), ("/a/{v:slug25}", "slug"))
    assert _who(forward, "/a/hello-world") == "slug"
    assert _who(reverse, "/a/hello-world") == "slug"
    assert _who(reverse, "/a/plain") == "str"


def test_a_custom_converter_that_declares_nothing_is_treated_as_str():
    """The only safe assumption about a pattern the framework cannot see."""

    class OpaqueConverter(_Converter):
        __slots__ = ()

        def match(self, value):
            return True, value

    register_converter("opaque", OpaqueConverter)
    assert OpaqueConverter.specificity == _Converter.specificity


def test_a_custom_subclass_inherits_the_specificity_it_narrows():
    """The old exact-type lookup demoted a subclass of int to the widest score."""
    from veloce.routing.converters import IntConverter

    class EvenIntConverter(IntConverter):
        __slots__ = ()

        def match(self, value):
            ok, coerced = super().match(value)
            return (ok and coerced % 2 == 0), coerced

    register_converter("evenint", EvenIntConverter)
    assert EvenIntConverter.specificity == IntConverter.specificity
    app = _app(("/a/{s:str}", "str"), ("/a/{n:evenint}", "even"))
    assert _who(app, "/a/4") == "even"
    assert _who(app, "/a/3") == "str"


# ── the declaration itself ───────────────────────────────────────────


def test_every_built_in_converter_declares_a_specificity():
    """The gap that caused this: a converter nobody added to the table."""
    from veloce.routing.converters import _BUILTIN

    undeclared = [
        name
        for name, cls in _BUILTIN.items()
        if cls.specificity == _Converter.specificity and cls is not _BUILTIN["str"]
    ]
    assert undeclared == [], f"built-in converters with no declared specificity: {undeclared}"


def test_the_declared_order_is_the_intended_order():
    from veloce.routing.converters import _BUILTIN, AnyConverter

    by_name = {name: cls.specificity for name, cls in _BUILTIN.items()}
    # Correctness needs only one thing: everything more restrictive than `str`
    # is tried before it, and greedy `path` last.
    for name in ("uuid", "int", "date", "datetime", "time", "timedelta", "decimal", "float"):
        assert by_name[name] < by_name["str"], name
    assert by_name["str"] < by_name["path"]
    # `any` takes arguments, so the parser builds it rather than the registry
    # naming it - checked against the class instead.
    assert AnyConverter.specificity < by_name["str"]
    # Among the restrictive ones the order is a cost choice, not a correctness
    # one: disjoint converters never accept the same segment, so the cheap and
    # common `int` is tried before the date family.
    assert by_name["uuid"] < by_name["int"] < by_name["date"]
