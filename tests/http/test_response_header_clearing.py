"""Clearing a response header finds it under whatever casing it was stored.

Nine header properties read case-insensitively (`header_get`) and cleared
case-sensitively (`headers.pop(NAME, None)`). A getter and its own setter
disagreed, so `response.location = None` on a response whose header arrived as
`location` left the header in place and returned it on the next read:

    location   before=/old   after `= None` -> /old   headers={'location': '/old'}

The end-to-end shape is a handler that decides not to redirect after all:

    response.location = None
    response.status_code = 200
    -> raw headers still carry (b'location', b'/secret-internal')

Nothing in `src/veloce/` writes a non-canonical response header, so the trigger
is user or third-party code - most plausibly a proxy handler rebuilding a
`Response` from an upstream's headers, since HTTP/2 mandates lowercase field
names. The `= value` half was always harmless: both emit paths fold duplicate
field names and keep the last write.

`header_pop` is the same helper the `Vary`, `Allow`, and CORS sites use.
"""

from __future__ import annotations

import pytest

from tests._response_accessors import (
    CLEARABLE,
    CLEARABLE_CANONICAL,
    CLEARABLE_IDS,
    CLEARABLE_PROPS,
    CLEARABLE_STORED,
)
from veloce import Veloce
from veloce.http.response import Response
from veloce.testclient import TestClient

# ── clearing works under a non-canonical casing ──────────────────────


@pytest.mark.parametrize(("prop", "canonical", "value", "stored"), CLEARABLE, ids=CLEARABLE_IDS)
def test_clearing_removes_the_header(prop, canonical, value, stored):
    """The defect: the header survived and the getter kept returning it."""
    response = Response(body=b"x", headers={stored: value})
    assert getattr(response, prop) is not None
    setattr(response, prop, None)
    assert getattr(response, prop) is None
    assert not [k for k in response.headers if k.lower() == canonical.lower()]


@pytest.mark.parametrize(("prop", "canonical", "value"), CLEARABLE_CANONICAL, ids=CLEARABLE_IDS)
def test_clearing_works_under_the_canonical_casing_too(prop, canonical, value):
    """The case that already worked must keep working."""
    response = Response(body=b"x", headers={canonical: value})
    setattr(response, prop, None)
    assert getattr(response, prop) is None
    assert canonical not in response.headers


@pytest.mark.parametrize("prop", CLEARABLE_PROPS, ids=CLEARABLE_IDS)
def test_clearing_an_absent_header_is_a_no_op(prop):
    response = Response(body=b"x", headers={"X-Other": "keep"})
    setattr(response, prop, None)
    assert response.headers == {"X-Other": "keep"}


@pytest.mark.parametrize(("prop", "value", "stored"), CLEARABLE_STORED, ids=CLEARABLE_IDS)
def test_clearing_removes_only_the_named_header(prop, value, stored):
    response = Response(body=b"x", headers={stored: value, "X-Other": "keep"})
    setattr(response, prop, None)
    assert response.headers.get("X-Other") == "keep"


@pytest.mark.parametrize(("prop", "value", "stored"), CLEARABLE_STORED, ids=CLEARABLE_IDS)
def test_setting_a_value_over_a_non_canonical_casing_still_reads_back(prop, value, stored):
    """The `= value` half, which was always fine - pinned so it stays that way.

    A round trip rather than a literal: each property parses its own header, so
    writing back what it read is the assignment every one of them accepts.
    """
    response = Response(body=b"x", headers={stored: value})
    setattr(response, prop, getattr(response, prop))
    assert getattr(response, prop) is not None


# ── end to end: cancelling a redirect ────────────────────────────────


def test_a_cleared_location_does_not_reach_the_client():
    """The shape that made this matter: the header outlived the decision."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        response = Response(body=b"", content_type="text/plain")
        # As an upstream or a proxy layer would have written it.
        response.headers["location"] = "/secret-internal"
        response.location = None
        return response

    response = TestClient(app).get("/x")
    assert not [k for k in response.headers if k.lower() == "location"]
    assert "secret-internal" not in response.text


def test_a_cleared_header_is_absent_from_the_encoded_wire():
    """The native transport encodes from the same dict."""
    response = Response(body=b"x", content_type="text/plain", headers={"age": "5"})
    response.age = None
    assert b"Age" not in response.encode()
    assert b"age" not in response.encode()


def test_a_kept_header_still_reaches_the_wire():
    """The negative: clearing one must not take the others with it."""
    response = Response(
        body=b"x", content_type="text/plain", headers={"age": "5", "vary": "Origin"}
    )
    response.age = None
    encoded = response.encode()
    assert b"Vary: Origin" in encoded or b"vary: Origin" in encoded
