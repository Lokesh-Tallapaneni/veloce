"""Request convenience accessors — cookies, url, base_url, is_json, etc."""

from __future__ import annotations

import pytest

from tests.conftest import make_request


class TestRequestEnhancements:
    def test_cookies_parsing(self):
        req = make_request(headers={"cookie": "session=abc123; theme=dark"})
        assert req.cookies["session"] == "abc123"
        assert req.cookies["theme"] == "dark"

    def test_url_construction(self):
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

    def test_base_url(self):
        req = make_request(headers={"host": "api.example.com"})
        assert req.base_url == "http://api.example.com"

    def test_is_json(self):
        req = make_request(headers={"content-type": "application/json"})
        assert req.is_json is True

    def test_is_form(self):
        req = make_request(headers={"content-type": "application/x-www-form-urlencoded"})
        assert req.is_form is True

    def test_user_agent(self):
        req = make_request(headers={"user-agent": "Mozilla/5.0"})
        assert req.user_agent == "Mozilla/5.0"

    def test_content_length(self):
        req = make_request(headers={"content-length": "42"})
        assert req.content_length == 42

    @pytest.mark.asyncio
    async def test_text(self):
        req = make_request(body=b"hello world")
        text = await req.text()
        assert text == "hello world"

    def test_authorization(self):
        req = make_request(headers={"authorization": "Bearer token123"})
        assert req.authorization == "Bearer token123"
