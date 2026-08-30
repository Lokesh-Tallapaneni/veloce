"""`Request.url` and `Request.base_url` — the constructed URL accessors.

The rest of what this module carried (cookies, is_json, is_form, text,
authorization) is covered by the module named for each of those accessors:
`test_cookies.py`, `test_request_mimetype.py`, `test_request_is_multipart_form.py`,
`test_request_data_stream.py` and `test_request_auth.py`. `user_agent` and
`content_length` moved to `test_request_aliases.py` beside the other
header-derived aliases.
"""

from __future__ import annotations

from tests.conftest import make_request


def test_url_construction():
    req = make_request(
        path="/api/v1/users",
        query_string="page=1",
        headers={"host": "example.com:8080"},
    )
    url = req.url
    assert url.host == "example.com"
    assert url.port == 8080
    assert url.path == "/api/v1/users"
    assert str(url) == "http://example.com:8080/api/v1/users?page=1"


def test_base_url():
    req = make_request(headers={"host": "api.example.com"})
    assert req.base_url == "http://api.example.com"
