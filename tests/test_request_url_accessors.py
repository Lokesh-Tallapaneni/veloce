"""URL accessors on Request — full_path, url_root, host_url."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Request


def _req(path: str = "/", query: str = "", host: str = "example.com") -> Request:
    return make_request(
        method="GET",
        path=path,
        query_string=query,
        headers={"host": host},
        body=b"",
    )


# ── Q14: full_path ────────────────────────────────────────────────────


def test_full_path_with_query_string():
    req = _req("/users", "a=1&b=2")
    assert req.full_path == "/users?a=1&b=2"


def test_full_path_always_contains_question_mark_even_when_empty():
    """`full_path` always includes `?`, even when the query string
    is empty — distinguishes it from `path`."""
    req = _req("/users", "")
    assert req.full_path == "/users?"


# ── Q15: url_root + host_url ──────────────────────────────────────────


def test_url_root_no_path_or_query():
    req = _req("/users/42", "x=1", host="api.example.com")
    assert req.url_root == "http://api.example.com/"


def test_url_root_trailing_slash():
    req = _req("/", "", host="x.test")
    assert req.url_root.endswith("/")


def test_host_url_aliases_url_root():
    """Veloce exposes both `url_root` and `host_url`; they return the same
    value."""
    req = _req("/x", "", host="api.example.com")
    assert req.host_url == req.url_root


def test_url_root_respects_https_via_x_forwarded_proto():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={"host": "x.test", "x-forwarded-proto": "https"},
        body=b"",
    )
    assert req.url_root == "https://x.test/"


def test_url_root_includes_port_when_non_default():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={"host": "x.test:9000"},
        body=b"",
    )
    assert req.url_root == "http://x.test:9000/"
