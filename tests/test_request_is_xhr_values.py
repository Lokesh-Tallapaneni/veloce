"""Request.is_xhr and Request.values."""

from __future__ import annotations

import pytest

from veloce import Request


def _req(
    query: str = "",
    body: bytes = b"",
    headers: dict | None = None,
) -> Request:
    return Request(
        method="POST",
        path="/x",
        query_string=query,
        headers=headers or {},
        body=body,
    )


# ── is_xhr ───────────────────────────────────────────────────────────


def test_is_xhr_true_for_xmlhttprequest_header():
    r = _req(headers={"x-requested-with": "XMLHttpRequest"})
    assert r.is_xhr is True


def test_is_xhr_case_insensitive():
    r = _req(headers={"x-requested-with": "xmlhttprequest"})
    assert r.is_xhr is True


def test_is_xhr_false_without_header():
    assert _req().is_xhr is False


def test_is_xhr_false_with_other_value():
    assert _req(headers={"x-requested-with": "Something"}).is_xhr is False


# ── values: query + form merged ──────────────────────────────────────


@pytest.mark.asyncio
async def test_values_includes_query_string():
    r = _req(query="a=1&b=2")
    v = await r.values()
    assert v.get("a") == "1"
    assert v.get("b") == "2"


@pytest.mark.asyncio
async def test_values_includes_form_body():
    r = _req(
        body=b"x=10&y=20",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    v = await r.values()
    assert v.get("x") == "10"
    assert v.get("y") == "20"


@pytest.mark.asyncio
async def test_values_merges_query_and_form():
    r = _req(
        query="src=q",
        body=b"src=f",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    v = await r.values()
    # Both values preserved — caller can pick via getall().
    assert v.getall("src") == ["q", "f"]
