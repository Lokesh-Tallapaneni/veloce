"""response_class= in the route decorator."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import HTMLResponse, Request, Veloce


class TestResponseClass:
    async def test_html_response_class(self):
        app = Veloce(openapi_url=None)

        @app.get("/page", response_class=HTMLResponse)
        async def page(request: Request):
            return "<h1>Hello</h1>"

        resp = await app.handle_request(make_request(path="/page"))
        assert b"<h1>Hello</h1>" in resp.body
        assert "text/html" in resp.content_type
