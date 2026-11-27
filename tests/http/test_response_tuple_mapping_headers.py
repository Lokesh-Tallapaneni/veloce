"""A `(body, headers)` return accepts any mapping, not only a plain `dict`.

`_unpack_response_tuple` decided between a status and headers with
`isinstance(second, (dict, list, tuple))`. `Headers` is a `CIMultiDict` and not
a `dict` subclass, so the framework's own header type - a public top-level
export, and what `Request.headers` hands back - fell through to `int(second)`:

    return {"ok": True}, Headers({"X-Tag": "v"})   ->  500

The same object passed to a constructor has always worked, because
`Response(headers=...)` copies through `dict(headers)`. So the shape a reader is
told to use in `docs/guide/helpers.md` failed on exactly one of the two ways to
express it, and the suite covered only the plain-dict form.

`CHANGELOG` records the iterable-of-pairs half of this being fixed - "a handler
may return `(body, header_list)`; dispatch read the pair list as a status and
answered 500" - and the mapping half was missed in the same pass.

The rule is now the one the docstring always stated: headers are a mapping or an
iterable of pairs, and everything else names a status. `str` and `bytes` are
iterable but name a status, so they stay on the status side.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest

from veloce import Headers, Veloce, make_response
from veloce._internal import _unpack_response_tuple
from veloce.testclient import TestClient

TAG = "X-Tag"


def _headers_of(second: object) -> object:
    """What the table reads `second` as, when it reads it as headers."""
    unpacked = _unpack_response_tuple(("body", second))
    assert unpacked is not None
    _body, code, headers = unpacked
    assert code is None, f"read as a status ({code!r}) rather than headers"
    return headers


def _status_of(second: object) -> object:
    unpacked = _unpack_response_tuple(("body", second))
    assert unpacked is not None
    _body, code, headers = unpacked
    assert headers is None, f"read as headers ({headers!r}) rather than a status"
    return code


# ── the mapping shapes ───────────────────────────────────────────────


def test_a_plain_dict_is_headers():
    """The control: the one shape that always worked."""
    assert _headers_of({TAG: "d"}) == {TAG: "d"}


def test_a_headers_object_is_headers():
    """The regression: `Headers` is not a `dict`, so it read as a status."""
    supplied = Headers({TAG: "h"})

    assert _headers_of(supplied) is supplied


def test_a_headers_object_reaches_the_response():
    """End to end, through dispatch rather than the table alone."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return {"ok": True}, Headers({TAG: "h"})

    response = TestClient(app).get("/x")

    assert response.status_code == 200
    assert response.headers.get(TAG.lower()) == "h"


def test_make_response_takes_a_headers_object_too():
    """The other door onto the same table."""
    response = make_response(({"ok": True}, Headers({TAG: "m"})))

    assert response.status_code == 200
    assert response.headers.get(TAG) == "m"


def test_the_three_element_form_takes_one_as_well():
    """`(body, status, headers)` reads its third element without this test, but
    the shapes should not diverge."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return {"ok": True}, 201, Headers({TAG: "three"})

    response = TestClient(app).get("/x")

    assert response.status_code == 201
    assert response.headers.get(TAG.lower()) == "three"


# ── the iterable-of-pairs shapes the docstring also promises ─────────


@pytest.mark.parametrize(
    ("label", "second"),
    [
        ("list", [(TAG, "l")]),
        ("tuple", ((TAG, "t"),)),
        ("items view", {TAG: "i"}.items()),
    ],
)
def test_an_iterable_of_pairs_is_headers(label: str, second: object):
    assert _headers_of(second) is second


def test_a_generator_of_pairs_is_headers():
    """Consumed once, so it is checked by what reaches the response."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return {"ok": True}, ((name, value) for name, value in [(TAG, "g")])

    response = TestClient(app).get("/x")

    assert response.status_code == 200
    assert response.headers.get(TAG.lower()) == "g"


# ── the status side must not move ────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "second", "expected"),
    [
        ("int", 201, 201),
        ("HTTPStatus", HTTPStatus.CREATED, HTTPStatus.CREATED),
        ("numeric string", "404", 404),
        ("float", 201.0, 201),
        ("bool", True, True),
    ],
)
def test_a_status_is_still_a_status(label: str, second: object, expected: object):
    """`str` and `bytes` are iterable; reading them as pairs would break this."""
    assert _status_of(second) == expected


def test_a_status_still_reaches_the_response():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return {"ok": True}, 201

    assert TestClient(app).get("/x").status_code == 201


def test_a_tuple_of_another_length_is_still_data():
    """The table answers `None` for anything that is not a response tuple."""
    assert _unpack_response_tuple(("a",)) is None
    assert _unpack_response_tuple(("a", 1, {}, "extra")) is None
