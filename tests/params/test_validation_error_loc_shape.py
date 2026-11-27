"""Every validation-error path reports `loc` in the shape the schema publishes.

The Pydantic-error-to-`RequestValidationError` remap is written out at four
sites in `dependency.py`, and the copies had drifted: three flattened each
`loc` part with `str(part)` and the fourth passed it through. The same logical
error therefore reported an array index as `1` or `"1"` depending only on which
body path happened to handle the request:

    {"lines": [{"qty": 1}, {"qty": "bad"}]}

    a Pydantic body model -> ["body", "lines", 1, "qty"]
    an adapted body model -> ["body", "lines", "1", "qty"]
    an embedded body      -> ["body", "lines", "1", "qty"]

The published `ValidationError` component settles which side is wrong: its
`loc` items are `anyOf: [string, integer]`, so an integer index is part of the
documented contract and the three stringifying sites made that alternative
unreachable. They now all pass the part through.

A client generated from the schema branches on that union, so this is the
"two doors disagree" shape again: the document promises an integer the runtime
never sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pytest
from pydantic import BaseModel

from veloce import Body, Veloce
from veloce.testclient import TestClient


class Line(BaseModel):
    qty: int


class Order(BaseModel):
    lines: list[Line]


@dataclass
class AdaptedOrder:
    lines: list[Line]


class Keyed(BaseModel):
    counts: dict[str, int]


BAD = {"lines": [{"qty": 1}, {"qty": "bad"}]}


@pytest.fixture
def client() -> TestClient:
    app = Veloce(openapi_url=None)

    @app.post("/pydantic")
    async def pydantic_body(order: Order):
        return {}

    @app.post("/adapted")
    async def adapted_body(order: AdaptedOrder):
        return {}

    @app.post("/embedded")
    async def embedded_body(order: Annotated[Order, Body(embed=True)]):
        return {}

    return TestClient(app)


def _loc(client: TestClient, path: str):
    body = {"order": BAD} if path == "/embedded" else BAD
    return client.post(path, json=body).json()["detail"][0]["loc"]


PATHS = ["/pydantic", "/adapted", "/embedded"]


# ── an array index survives as an integer ────────────────────────────


@pytest.mark.parametrize("path", PATHS)
def test_an_array_index_is_reported_as_an_integer(path, client):
    """The defect: two of these three flattened the index to `"1"`."""
    assert _loc(client, path) == ["body", "lines", 1, "qty"]


@pytest.mark.parametrize("path", PATHS)
def test_an_array_index_is_not_a_string(path, client):
    """Stated separately, because `1 == True` style coercions make the equality
    above less pointed than it looks."""
    index = _loc(client, path)[2]
    assert isinstance(index, int) and not isinstance(index, bool)


def test_every_body_path_reports_the_same_loc(client):
    """The property the four hand-written copies exist to satisfy."""
    locs = {tuple(_loc(client, path)) for path in PATHS}
    assert len(locs) == 1, locs


# ── and the document says an integer is possible ─────────────────────


def test_the_published_schema_admits_an_integer_loc_part():
    """The reason the integer is the correct side of the disagreement."""
    app = Veloce()

    @app.post("/x")
    async def x(order: Order):
        return {}

    loc = app.openapi()["components"]["schemas"]["ValidationError"]["properties"]["loc"]
    admitted = {entry["type"] for entry in loc["items"]["anyOf"]}
    assert admitted == {"string", "integer"}


def test_a_reported_loc_validates_against_the_published_schema(client):
    """The two doors, checked against each other rather than asserted apart."""
    app = Veloce()

    @app.post("/x")
    async def x(order: Order):
        return {}

    loc = app.openapi()["components"]["schemas"]["ValidationError"]["properties"]["loc"]
    admitted = {entry["type"] for entry in loc["items"]["anyOf"]}
    kinds = {"string": str, "integer": int}
    for path in PATHS:
        for part in _loc(client, path):
            assert any(isinstance(part, kinds[name]) for name in admitted), (path, part)


# ── string keys stay strings ─────────────────────────────────────────
#
# The negative. A "fix" that turned every part into an int, or that stopped
# converting anything at all, would pass the assertions above.


@pytest.mark.parametrize("path", PATHS)
def test_field_names_are_still_strings(path, client):
    parts = _loc(client, path)
    assert isinstance(parts[0], str)
    assert isinstance(parts[1], str)
    assert isinstance(parts[3], str)


@pytest.mark.parametrize("path", PATHS)
def test_the_loc_still_opens_with_body(path, client):
    assert _loc(client, path)[0] == "body"


def test_a_flat_body_error_is_unaffected(client):
    """No index involved, so this shape was already consistent - and stays so."""
    resp = client.post("/pydantic", json={"lines": "nope"})
    assert resp.json()["detail"][0]["loc"] == ["body", "lines"]


def test_a_numeric_dict_key_is_still_a_string():
    """The information the stringifying sites destroyed: a key `"1"` and an
    index `1` are different locations, and must not both report as `"1"`."""
    app = Veloce(openapi_url=None)

    @app.post("/keyed")
    async def keyed(payload: Keyed):
        return {}

    loc = TestClient(app).post("/keyed", json={"counts": {"1": "bad"}}).json()["detail"][0]["loc"]
    assert loc == ["body", "counts", "1"]
    assert isinstance(loc[2], str)
