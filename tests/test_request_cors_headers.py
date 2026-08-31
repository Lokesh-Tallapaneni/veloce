"""Request.origin + CORS preflight header accessors."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Request


def _req(headers: dict[str, str]) -> Request:
    return make_request(method="OPTIONS", path="/", query_string="", headers=headers, body=b"")


# ── origin ──────────────────────────────────────────────────────────


def test_origin_none_when_absent():
    assert _req({}).origin is None


def test_origin_reads_header():
    assert _req({"Origin": "https://app.example.com"}).origin == ("https://app.example.com")


# ── access_control_request_method ───────────────────────────────────


def test_acrm_none_when_absent():
    assert _req({}).access_control_request_method is None


def test_acrm_reads_header():
    req = _req({"Access-Control-Request-Method": "POST"})
    assert req.access_control_request_method == "POST"


# ── access_control_request_headers ──────────────────────────────────


def test_acrh_empty_when_absent():
    assert _req({}).access_control_request_headers == []


def test_acrh_parses_header_list():
    req = _req({"Access-Control-Request-Headers": "Content-Type, X-Token"})
    assert req.access_control_request_headers == ["content-type", "x-token"]


def test_acrh_skips_blank_entries():
    req = _req({"Access-Control-Request-Headers": "a, , b,"})
    assert req.access_control_request_headers == ["a", "b"]
