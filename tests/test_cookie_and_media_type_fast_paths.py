"""Two parsers that skip work they can see is unnecessary.

`iter_cookies` decoded every value whether or not it carried an escape, and
tested each segment for `=` before partitioning on it — two scans where one
does. `_parse_content_type` built a generator and a dict of parameters for a
`Content-Type` that declares none.

Both guards key on something `partition` or a substring test already knows. The
risk is that the guarded branch and the full one drift apart, so these pin the
two against each other over the shapes a real header can take.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest

from veloce import Request, Response
from veloce._header_parsing import unquote_value
from veloce.http.cookies import dump_cookie, iter_cookies, parse_cookie

# ── Cookies ──────────────────────────────────────────────────────────

#: Every shape a `Cookie:` header can take: absent, empty, blank values, no
#: `=`, repeated names, quoted values, percent escapes, padding, and both
#: guard markers in and out of the value.
_COOKIE_HEADERS = [
    None,
    "",
    "a=1",
    "a=1; b=2",
    " a = 1 ",
    "a=",
    "=1",
    "=",
    ";",
    ";;",
    "a=1;;b=2",
    "a",
    "a;b=2",
    'a="1"',
    'a=" 1 "',
    "a=%20",
    "a=%zz",
    "a=%",
    "a=1; a=2",
    "a=1;a=2;a=3",
    'a="quoted; inside"',
    "a=b=c",
    "a==b",
    "  a=1  ;  b=2  ",
    "a=\t1",
    "session=abc123; theme=dark; lang=en-GB",
    'csrf="tok%20en"',
    "a=1 ",
    " =1",
    "a =",
    'a=""',
    'a="',
    "a=ключ",
    "a=%C3%A9",
    "a=+",
    "a=a+b",
    "a=;b=2",
]


def _unguarded(header: str | None) -> list[tuple[str, str]]:
    """The parse without the guard: decode every value, unconditionally."""
    if not header:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in header.split(";"):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        if name in seen:
            continue
        seen.add(name)
        out.append((name, unquote(unquote_value(value))))
    return out


@pytest.mark.parametrize("header", _COOKIE_HEADERS)
def test_the_guarded_cookie_parse_matches_decoding_every_value(header):
    assert list(iter_cookies(header)) == _unguarded(header)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "plain"),
        ("a b", "a b"),
        ("a%20b", "a b"),
        ("a+b", "a+b"),  # `+` is not a space in a cookie value
        ("100%", "100%"),
        ("%C3%A9", "é"),
        ("", ""),
    ],
)
def test_a_cookie_value_decodes_the_same_either_side_of_the_guard(value, expected):
    assert parse_cookie(f"k={value}") == {"k": expected}


def test_a_quoted_value_is_still_unquoted():
    """The `"` marker is in the guard precisely so this keeps working."""
    assert parse_cookie('k="quoted"') == {"k": "quoted"}
    # The quotes are the delimiter; whitespace inside them is part of the value
    # (RFC 6265 Sec. 4.1.1 quoted-string).
    assert parse_cookie('k=" padded "') == {"k": " padded "}


def test_a_value_this_project_wrote_round_trips():
    """`dump_cookie` is the encoder the guard's markers describe."""
    for raw in ["plain", "a b", "a;b", 'a"b', "é", "a=b", "100%"]:
        header = dump_cookie("k", raw).split(";")[0]
        assert parse_cookie(header) == {"k": raw}


def test_the_first_of_a_repeated_name_still_wins():
    assert parse_cookie("a=1; a=2; a=3") == {"a": "1"}


def test_padding_around_a_name_and_value_is_still_trimmed():
    assert parse_cookie("  a  =  1  ;  b=2") == {"a": "1", "b": "2"}


def test_a_segment_with_no_equals_is_still_skipped():
    assert parse_cookie("novalue; a=1") == {"a": "1"}


# ── Media type ───────────────────────────────────────────────────────


def _request_with(content_type: str) -> Request:
    return Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": content_type} if content_type else {},
        body=b"",
    )


@pytest.mark.parametrize(
    ("content_type", "mimetype", "params"),
    [
        ("application/json", "application/json", {}),
        ("application/json; charset=utf-8", "application/json", {"charset": "utf-8"}),
        ("APPLICATION/JSON", "application/json", {}),
        ("  text/plain  ", "text/plain", {}),
        ("text/plain;", "text/plain", {}),
        ("text/plain; ", "text/plain", {}),
        ("text/html;charset=iso-8859-1", "text/html", {"charset": "iso-8859-1"}),
        ("", "", {}),
    ],
)
def test_a_content_type_parses_to_the_same_mimetype_and_params(content_type, mimetype, params):
    request = _request_with(content_type)
    assert request.mimetype == mimetype
    assert request.mimetype_params == params


def test_a_parameterless_type_yields_an_empty_mapping_not_a_shared_one():
    """The guard returns a fresh dict; a shared one would leak between requests."""
    first = _request_with("application/json")
    second = _request_with("text/plain")
    first.mimetype_params["injected"] = "x"
    assert second.mimetype_params == {}


@pytest.mark.parametrize(
    ("content_type", "mimetype", "params"),
    [
        ("application/json", "application/json", {}),
        ("application/json; charset=utf-8", "application/json", {"charset": "utf-8"}),
        ("text/plain;", "text/plain", {}),
    ],
)
def test_a_response_parses_its_content_type_the_same_way(content_type, mimetype, params):
    response = Response(body=b"", content_type=content_type)
    assert response.mimetype == mimetype
    assert response.mimetype_params == params


def test_a_response_reparses_when_its_content_type_is_reassigned():
    """The guard must not make the value-keyed cache serve a stale answer."""
    response = Response(body=b"", content_type="application/json; charset=utf-8")
    assert response.mimetype_params == {"charset": "utf-8"}
    response.content_type = "text/plain"
    assert response.mimetype == "text/plain"
    assert response.mimetype_params == {}
