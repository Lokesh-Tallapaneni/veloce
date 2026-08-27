"""`Request.content_language` / `pragma` / `max_forwards` header accessors.

Was `test_request_misc_headers.py`: "misc" names nothing, and a module that
can absorb anything will.
"""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Request


def _req(headers: dict[str, str]) -> Request:
    return make_request(method="GET", path="/", query_string="", headers=headers, body=b"")


# ── content_language ────────────────────────────────────────────────


def test_content_language_empty_when_absent():
    assert _req({}).content_language == ""


def test_content_language_reads_header():
    assert _req({"Content-Language": "en-US"}).content_language == "en-US"


def test_content_language_multiple_tags_preserved():
    assert _req({"Content-Language": "en, fr, de"}).content_language == "en, fr, de"


# ── pragma ──────────────────────────────────────────────────────────


def test_pragma_empty_when_absent():
    assert _req({}).pragma == ""


def test_pragma_no_cache_lowercased():
    assert _req({"Pragma": "No-Cache"}).pragma == "no-cache"


# ── max_forwards ────────────────────────────────────────────────────


def test_max_forwards_none_when_absent():
    assert _req({}).max_forwards is None


def test_max_forwards_parses_int():
    assert _req({"Max-Forwards": "5"}).max_forwards == 5


def test_max_forwards_none_when_non_numeric():
    assert _req({"Max-Forwards": "abc"}).max_forwards is None


def test_max_forwards_zero():
    assert _req({"Max-Forwards": "0"}).max_forwards == 0
