"""Top-level `redirect()` helper."""

from __future__ import annotations

import pytest

from veloce import RedirectResponse, Request, Veloce, redirect


def test_default_status_code_is_302():
    """`redirect()` defaults to a 302 Found."""
    resp = redirect("/somewhere")
    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/somewhere"


def test_explicit_status_code_passes_through():
    resp = redirect("/x", code=301)
    assert resp.status_code == 301
    resp = redirect("/x", code=307)
    assert resp.status_code == 307


def test_extra_headers_merged():
    resp = redirect("/x", headers={"Vary": "Accept"})
    assert resp.headers["Vary"] == "Accept"
    assert resp.headers["Location"] == "/x"


def test_body_is_empty():
    resp = redirect("/x")
    assert resp.body == b""


@pytest.mark.asyncio
async def test_returnable_from_handler():
    """Handlers can `return redirect(...)` and it round-trips correctly."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/from")
    async def go():
        return redirect("/to", code=303)

    resp = await app.handle_request(
        Request(method="GET", path="/from", query_string="", headers={}, body=b"")
    )
    assert resp.status_code == 303
    assert resp.headers["Location"] == "/to"
