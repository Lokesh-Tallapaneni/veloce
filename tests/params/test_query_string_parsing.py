"""`_parse_qs_pairs` — the shared query-string / urlencoded-body parser.

`urllib.parse.parse_qsl` calls `unquote_plus` twice per field, and each call
allocates a replaced string before scanning for `%`. A value carrying neither
`%` nor `+` gives all of that nothing to do, which is the shape of most query
strings and most urlencoded bodies, so those take a guarded fast path instead.

The fast path is not an approximation, and that is the whole risk: these pin it
against `parse_qsl` itself over the shapes a request can actually carry, so the
two can never come to disagree about what a query string means.
"""

from __future__ import annotations

from urllib.parse import parse_qsl

import pytest

from veloce import Veloce
from veloce.exceptions import RequestEntityTooLarge, RequestURITooLong
from veloce.http.datastructures import _MAX_QUERY_FIELDS, QueryParams, _parse_qs_pairs
from veloce.testclient import TestClient

#: Every shape worth pinning: empty, blank values, missing `=`, empty segments,
#: repeated keys, `=` inside a value, separators that are not separators here,
#: whitespace, non-ASCII, and every escape form the guard keys on.
_SHAPES = [
    "",
    "a",
    "a=",
    "=a",
    "=",
    "&",
    "&&",
    "a=1&",
    "&a=1",
    "a=1&&b=2",
    "a=b=c",
    "a==b",
    "abc",
    "a=1;b=2",
    "a=1&b",
    "a&b",
    ";",
    "a;b",
    "a=1&a=2&a=3",
    " a = 1 ",
    "a=1 ",
    "\ta=1",
    "a=\n1",
    "ключ=значение",
    "a=é",
    "a[]=1&a[]=2",
    "a.b=1",
    "a=%",
    "a=%zz",
    "a=%2",
    "%=1",
    "+=1",
    "a=+",
    "a=1+2",
    "a=%20",
    "a=%2B",
    "q=h%C3%A9llo+world&page=2",
    "q=hello&page=2&sort=rel",
]


@pytest.mark.parametrize("value", _SHAPES)
def test_it_agrees_with_the_stdlib_parser(value: str):
    assert _parse_qs_pairs(value, None) == parse_qsl(value, keep_blank_values=True)


@pytest.mark.parametrize("value", _SHAPES)
def test_it_agrees_with_the_stdlib_parser_under_a_field_cap(value: str):
    expected = parse_qsl(value, keep_blank_values=True, max_num_fields=1000)
    assert _parse_qs_pairs(value, 1000) == expected


# ── The field cap is enforced at the same boundary ───────────────────


@pytest.mark.parametrize("count", [1023, 1024, 1025, 2000])
def test_the_field_cap_fires_where_the_stdlib_parser_fires(count: int):
    """`parse_qsl` counts `1 + value.count("&")`, and so must the fast path."""
    value = "&".join(f"k{i}=v{i}" for i in range(count))

    def raised(parse) -> bool:
        try:
            parse()
        except ValueError:
            return True
        return False

    assert raised(lambda: _parse_qs_pairs(value, 1024)) == raised(
        lambda: parse_qsl(value, keep_blank_values=True, max_num_fields=1024)
    )


def test_the_cap_counts_empty_segments_the_stdlib_way():
    """`a=1&&&&` is five fields to the cap even though four are skipped."""
    value = "a=1&&&&"
    assert _parse_qs_pairs(value, 5) == parse_qsl(value, keep_blank_values=True, max_num_fields=5)
    with pytest.raises(ValueError):
        _parse_qs_pairs(value, 4)
    with pytest.raises(ValueError):
        parse_qsl(value, keep_blank_values=True, max_num_fields=4)


def test_no_cap_means_no_cap():
    value = "&".join(f"k{i}=v{i}" for i in range(5000))
    assert len(_parse_qs_pairs(value, None)) == 5000


# ── The callers' limits still surface as HTTP errors ─────────────────


def test_an_overlong_query_string_is_still_a_414():
    """The parser changed; the status the caller translates it to did not."""
    with pytest.raises(RequestURITooLong):
        QueryParams.from_query_string("&".join(f"k{i}=v" for i in range(_MAX_QUERY_FIELDS + 1)))


def test_a_query_string_at_the_limit_is_accepted():
    params = QueryParams.from_query_string("&".join(f"k{i}=v" for i in range(_MAX_QUERY_FIELDS)))
    assert len(params) == _MAX_QUERY_FIELDS


def test_an_overlong_form_body_is_still_a_413():
    app = Veloce(openapi_url=None)
    app.config["MAX_FORM_PARTS"] = 3

    @app.post("/f")
    async def take(request):
        try:
            await request.form()
        except RequestEntityTooLarge:
            return {"refused": True}
        return {"refused": False}

    client = TestClient(app)
    assert client.post("/f", data={f"k{i}": "v" for i in range(4)}).json() == {"refused": True}
    assert client.post("/f", data={f"k{i}": "v" for i in range(3)}).json() == {"refused": False}


# ── End to end, through both call sites ──────────────────────────────


def _echo_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/q")
    async def query(request):
        return {"items": [list(pair) for pair in request.query_params.items()]}

    @app.post("/f")
    async def form(request):
        data = await request.form()
        return {"items": [list(pair) for pair in data.items()]}

    return app


@pytest.mark.parametrize(
    "query_string",
    ["q=hello&page=2", "q=h%C3%A9llo+world", "a=1&a=2", "blank=", "a=b=c", "tag=a%26b"],
)
def test_a_query_string_round_trips_through_a_real_request(query_string: str):
    client = TestClient(_echo_app())
    expected = [list(pair) for pair in parse_qsl(query_string, keep_blank_values=True)]
    assert client.get(f"/q?{query_string}").json()["items"] == expected


@pytest.mark.parametrize(
    "body",
    ["name=widget&qty=3", "name=h%C3%A9llo+world", "a=1&a=2", "blank=", "note=a%3Db"],
)
def test_a_urlencoded_body_round_trips_through_a_real_request(body: str):
    client = TestClient(_echo_app())
    expected = [list(pair) for pair in parse_qsl(body, keep_blank_values=True)]
    response = client.post(
        "/f",
        content=body.encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.json()["items"] == expected


def test_a_plus_in_a_form_body_is_still_a_space():
    """The guard exists so this keeps working; it is the reason for the `+` check."""
    client = TestClient(_echo_app())
    response = client.post(
        "/f",
        content=b"q=hello+world",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.json()["items"] == [["q", "hello world"]]


def test_a_percent_escape_in_a_query_string_is_still_decoded():
    client = TestClient(_echo_app())
    assert client.get("/q?q=a%20b").json()["items"] == [["q", "a b"]]
