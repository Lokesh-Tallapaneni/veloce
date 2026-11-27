"""`float` is tried before `decimal`, because it is the more restrictive of the two.

`Converter.specificity` says "the lower value is tried first, so `/items/{id:int}`
beats `/items/{slug:str}` however they were declared" - lower means more
restrictive. `FloatConverter` requires a fractional part and `DecimalConverter`
makes it optional, so float accepts a strict subset of what decimal accepts:

          3  decimal= True  float=False
        3.5  decimal= True  float= True
         -2  decimal= True  float=False
      10.25  decimal= True  float= True

Float is therefore the more restrictive, and carried `41` against decimal's `40`
- so decimal was tried first, and a segment both accept went to the looser
converter. That inverts the file's own rule on the one pair where the two are
directly comparable.
"""

from __future__ import annotations

import decimal

from tests.conftest import make_request
from veloce import Veloce
from veloce.routing import converters
from veloce.routing.converters import DecimalConverter, FloatConverter


def test_float_accepts_a_strict_subset_of_decimal():
    """The premise: this is what makes float the more restrictive."""
    d, f = DecimalConverter(), FloatConverter()
    accepted_by_float = {v for v in ("3", "3.5", "-2", "10.25") if f.match(v)[0]}
    accepted_by_decimal = {v for v in ("3", "3.5", "-2", "10.25") if d.match(v)[0]}

    assert accepted_by_float < accepted_by_decimal


def test_float_sorts_ahead_of_decimal():
    """The rule the file states, applied to the pair that broke it."""
    assert FloatConverter.specificity < DecimalConverter.specificity


async def test_a_value_both_accept_goes_to_the_stricter_route():
    """The consequence, driven through the router rather than read off a number."""
    app = Veloce(openapi_url=None)
    seen: list[str] = []

    @app.get("/v/{value:float}")
    async def as_float(value: float):
        seen.append("float")
        return {"ok": True}

    @app.get("/v/{value:decimal}")
    async def as_decimal(value: decimal.Decimal):
        seen.append("decimal")
        return {"ok": True}

    await app.handle_request(make_request(path="/v/3.5"))

    assert seen == ["float"], "a segment both accept went to the looser converter"


async def test_a_value_only_decimal_accepts_still_reaches_it():
    """Ordering must not make the looser converter unreachable."""
    app = Veloce(openapi_url=None)
    seen: list[str] = []

    @app.get("/v/{value:float}")
    async def as_float(value: float):
        seen.append("float")
        return {"ok": True}

    @app.get("/v/{value:decimal}")
    async def as_decimal(value: decimal.Decimal):
        seen.append("decimal")
        return {"ok": True}

    await app.handle_request(make_request(path="/v/3"))

    assert seen == ["decimal"]


def test_every_specificity_is_distinct():
    """Two converters sharing a value make their relative order arbitrary.

    `str` and its greedy `path` sibling share `50` deliberately - they are the
    catch-alls and neither is more restrictive - so the check is that nothing
    *else* ties.
    """
    values: dict[int, list[str]] = {}
    for name in dir(converters):
        obj = getattr(converters, name)
        if isinstance(obj, type) and issubclass(obj, converters.Converter):
            if obj is converters.Converter:
                continue
            values.setdefault(obj.specificity, []).append(name)

    ties = {value: names for value, names in values.items() if len(names) > 1}

    assert set(ties) <= {50}, f"converters tie on specificity: {ties}"
