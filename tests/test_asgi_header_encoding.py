"""Building the ASGI header list: same output, less work per response.

`_build_asgi_headers` runs once per response and did two things per header that
it did not need to:

* built `f"{k} header value"` as the CRLF-guard's error label, for a string
  almost every call discards. `_reject_header_crlf` already takes
  `(value, what, suffix)` and joins them *only when raising* - its docstring
  names this exact caller - and the `_internal.py` twin already did it that way.
* called `k_lower.encode()` on names that are overwhelmingly the framework's own
  constants, re-encoding the same handful of strings on every response.

The encoding table is seeded from `_constants.HEADER_*` so it cannot drift from
them, and is never written at runtime, so an application using dynamic header
names cannot grow it without bound - those fall through to `.encode()`.

These tests exist because a lookup table in front of an encode is exactly the
shape that quietly changes output for the entries it misses. Every assertion is
about the bytes on the wire, not about the table.
"""

from __future__ import annotations

import pytest

from veloce import Response, Veloce
from veloce.app.asgi import _ENCODED_HEADER_NAMES, _build_asgi_headers
from veloce.testclient import TestClient


def _names(headers: dict) -> list[bytes]:
    entries, _ct, _cl = _build_asgi_headers(headers)
    return [name for name, _value in entries]


# ── names are lowercase bytes, table hit or miss ─────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "Content-Type",  # in the table
        "Content-Length",
        "Vary",
        "X-Totally-Custom",  # not in the table
        "x-already-lower",
        "X-MiXeD-CaSe",
        "A",
    ],
)
def test_a_header_name_is_emitted_as_lowercase_bytes(name):
    entries, _ct, _cl = _build_asgi_headers({name: "v"})
    assert entries == [(name.lower().encode("latin-1"), b"v")]


def test_a_name_in_the_table_encodes_identically_to_a_plain_encode():
    """The table must be a cache, not a second implementation."""
    for folded, encoded in _ENCODED_HEADER_NAMES.items():
        assert encoded == folded.encode("latin-1"), folded


def test_every_table_key_is_already_folded():
    assert all(key == key.lower() for key in _ENCODED_HEADER_NAMES)


def test_the_table_is_not_empty():
    """A table that silently held nothing would make the fast path dead and
    every assertion above vacuous."""
    assert len(_ENCODED_HEADER_NAMES) > 50
    assert "content-type" in _ENCODED_HEADER_NAMES


def test_a_name_missing_from_the_table_still_reaches_the_wire():
    """The miss path, end to end."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(body=b"ok", headers={"X-Not-In-Table": "yes"})

    assert TestClient(app).get("/x").headers["X-Not-In-Table"] == "yes"


# ── the CRLF guard still guards, and still names the field ───────────


@pytest.mark.parametrize("bad", ["a\rb", "a\nb", "a\x00b"])
def test_a_control_character_in_a_value_is_refused(bad):
    with pytest.raises(ValueError):
        _build_asgi_headers({"X-Custom": bad})


@pytest.mark.parametrize("bad", ["a\rb", "a\nb", "a\x00b"])
def test_a_control_character_in_a_name_is_refused(bad):
    with pytest.raises(ValueError):
        _build_asgi_headers({bad: "v"})


def test_the_refusal_still_names_the_offending_header():
    """The label is now joined inside the guard; the message must be unchanged.

    A cheaper label that stopped identifying the field would make a response
    -splitting bug much harder to locate, which is the whole point of raising
    instead of stripping.
    """
    with pytest.raises(ValueError) as excinfo:
        _build_asgi_headers({"X-Trace": "bad\rvalue"})
    message = str(excinfo.value)
    assert "X-Trace" in message
    assert "header value" in message


def test_the_name_refusal_names_the_field_too():
    with pytest.raises(ValueError) as excinfo:
        _build_asgi_headers({"X-Bad\rName": "v"})
    assert "header name" in str(excinfo.value).lower()


# ── the rest of the builder is unchanged ─────────────────────────────


def test_content_type_and_length_are_still_reported():
    _entries, has_ct, has_cl = _build_asgi_headers(
        {"Content-Type": "application/json", "Content-Length": "2"}
    )
    assert has_ct is True
    assert has_cl is True


def test_absent_content_headers_are_reported_absent():
    _entries, has_ct, has_cl = _build_asgi_headers({"Vary": "Origin"})
    assert has_ct is False
    assert has_cl is False


def test_a_second_spelling_overwrites_the_first():
    """Two spellings of one field must not both reach the wire."""
    entries, _ct, _cl = _build_asgi_headers(
        {"Content-Security-Policy": "a", "content-security-policy": "b"}
    )
    assert entries == [(b"content-security-policy", b"b")]


def test_set_cookie_is_still_multi_valued():
    from veloce._protocol_constants import SET_COOKIE_JOINER

    entries, _ct, _cl = _build_asgi_headers({"Set-Cookie": f"a=1{SET_COOKIE_JOINER}b=2"})
    assert [value for _name, value in entries] == [b"a=1", b"b=2"]
    assert {name for name, _value in entries} == {b"set-cookie"}


def test_header_order_is_preserved():
    assert _names({"B-Header": "1", "A-Header": "2", "Vary": "3"}) == [
        b"b-header",
        b"a-header",
        b"vary",
    ]


def test_a_response_round_trips_every_header(tmp_path):
    """End to end, through a real response."""
    app = Veloce(openapi_url=None)
    sent = {
        "X-One": "1",
        "Vary": "Origin",
        "Cache-Control": "no-store",
        "X-Custom-Trace": "abc",
    }

    @app.get("/x")
    async def x():
        return Response(body=b"ok", headers=dict(sent))

    resp = TestClient(app).get("/x")
    for key, value in sent.items():
        assert resp.headers[key] == value
