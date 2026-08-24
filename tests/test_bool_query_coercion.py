"""A boolean request value is read, or refused — never quietly read as False.

Veloce refuses `?page=abc` for an `int` on the stated grounds that a value
contradicting the declared type is the caller's mistake to correct. The boolean
branch did not honour that: it tested membership of a three-value truthy set and
returned `False` for everything else, so `?errors_only=ture` meant "no filter"
and nothing anywhere said so. `on` — the value an HTML checkbox submits — was
False for the same reason.

The accepted spellings are Pydantic's, so a bare `bool` parameter and one
carried on a model agree about the same query string.
"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, TypeAdapter

from veloce import Veloce
from veloce.testclient import TestClient

_TRUE = ["true", "True", "TRUE", "1", "on", "t", "y", "yes", "YES"]
_FALSE = ["false", "False", "FALSE", "0", "off", "f", "n", "no", "NO"]
#: Not booleans. Each used to be silently read as `False`.
_REFUSED = ["banana", "ture", "2", "-1", "", " ", "null", "None", "true ", "1.0"]


def _client() -> TestClient:
    app = Veloce(openapi_url=None)

    @app.get("/flag")
    async def flag(errors_only: bool = False) -> dict:
        return {"errors_only": errors_only}

    @app.get("/required")
    async def required(flag: bool) -> dict:
        return {"flag": flag}

    @app.get("/optional")
    async def optional(flag: bool | None = None) -> dict:
        return {"flag": flag}

    @app.get("/lit")
    async def lit(flag: Literal[True] = True) -> dict:
        return {"flag": flag}

    return TestClient(app)


# ── Accepted spellings ───────────────────────────────────────────────


@pytest.mark.parametrize("value", _TRUE)
def test_a_truthy_spelling_reads_as_true(value):
    assert _client().get(f"/flag?errors_only={value}").json() == {"errors_only": True}


@pytest.mark.parametrize("value", _FALSE)
def test_a_falsy_spelling_reads_as_false(value):
    assert _client().get(f"/flag?errors_only={value}").json() == {"errors_only": False}


@pytest.mark.parametrize("value", _TRUE + _FALSE)
def test_every_accepted_spelling_agrees_with_pydantic(value):
    """A bare parameter and a model field must not disagree about a query string."""
    expected = TypeAdapter(bool).validate_python(value)
    assert _client().get(f"/flag?errors_only={value}").json() == {"errors_only": expected}


def test_the_html_checkbox_value_is_true():
    """`on` is what a checked box submits; it used to mean False."""
    assert _client().get("/flag?errors_only=on").json() == {"errors_only": True}


# ── Refused values ───────────────────────────────────────────────────


@pytest.mark.parametrize("value", _REFUSED)
def test_a_value_that_is_not_a_boolean_is_refused(value):
    response = _client().get(f"/flag?errors_only={value}")
    assert response.status_code == 422


def test_the_refusal_names_the_parameter_and_its_location():
    """Same shape as the `int` refusal beside it."""
    detail = _client().get("/flag?errors_only=banana").json()["detail"]
    assert detail[0]["loc"] == ["query", "errors_only"]
    assert "errors_only" in detail[0]["msg"]


@pytest.mark.parametrize("value", _REFUSED)
def test_pydantic_refuses_the_same_values(value):
    """The refusal set is Pydantic's, not one invented here."""
    with pytest.raises(Exception):  # noqa: B017 - any validation failure
        TypeAdapter(bool).validate_python(value)


# ── The surrounding contract is unchanged ────────────────────────────


def test_an_omitted_parameter_still_takes_its_default():
    assert _client().get("/flag").json() == {"errors_only": False}


def test_a_required_boolean_is_still_required():
    assert _client().get("/required").status_code == 422


def test_a_required_boolean_still_reads_a_valid_value():
    assert _client().get("/required?flag=true").json() == {"flag": True}


def test_an_optional_boolean_omitted_is_none():
    assert _client().get("/optional").json() == {"flag": None}


def test_an_optional_boolean_still_refuses_a_non_boolean():
    assert _client().get("/optional?flag=banana").status_code == 422


class Flags(BaseModel):
    """Module level: the annotation is a string here, so it must resolve globally."""

    errors_only: bool


def test_a_body_model_reads_the_same_spellings():
    """The model path went through Pydantic already; the two now agree."""
    app = Veloce(openapi_url=None)

    @app.post("/m")
    async def take(flags: Flags) -> dict:
        return {"errors_only": flags.errors_only}

    client = TestClient(app)
    assert client.post("/m", json={"errors_only": "on"}).json() == {"errors_only": True}
    assert client.post("/m", json={"errors_only": "banana"}).status_code == 422


# ── `Literal` matching uses the same vocabulary ──────────────────────


@pytest.mark.parametrize("value", ["true", "on", "1", "yes"])
def test_a_literal_true_accepts_every_truthy_spelling(value):
    assert _client().get(f"/lit?flag={value}").json() == {"flag": True}


def test_a_literal_true_still_refuses_a_non_boolean():
    assert _client().get("/lit?flag=banana").status_code == 422
