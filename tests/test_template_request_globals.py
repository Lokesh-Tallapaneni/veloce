"""`request` and `csp_nonce` resolve in templates without context plumbing.

A template that writes `<script nonce="{{ csp_nonce }}">` used to render an
empty attribute unless the handler threaded `request` into the render context.
The browser then refused the inline script and nothing on the server side -
header, body, or log - recorded a failure, so the pages under test still
passed. Both names now resolve from the request being handled.

The handlers here declare **no** `request` parameter, deliberately: the claim
is that neither name needs threading, and a handler taking one would have
argued the opposite while ruff reported eleven unread arguments.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from veloce import CSPMiddleware, Veloce
from veloce.contrib.templating import Jinja2Templates, render_template_string
from veloce.http.response import StreamingResponse
from veloce.testclient import AsyncTestClient, TestClient

NONCE_IN_HEADER = re.compile(r"'nonce-([\w-]+)'")


def _armed_app() -> Veloce:
    """An app whose CSP policy carries a nonce placeholder.

    It took a `tmp_path` it never read: the templates are loaded by
    `Jinja2Templates`, which each caller constructs itself.
    """
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware, policy="script-src {nonce}")
    return app


def _header_nonce(response) -> str:
    match = NONCE_IN_HEADER.search(response.headers["content-security-policy"])
    assert match, response.headers.get("content-security-policy")
    return match.group(1)


def test_csp_nonce_resolves_without_threading_the_request(tmp_path: Path):
    (tmp_path / "p.html").write_text('<script nonce="{{ csp_nonce }}">x</script>')
    app = _armed_app()
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/")
    async def index():
        return templates.TemplateResponse("p.html", {})

    response = TestClient(app).get("/")
    assert f'nonce="{_header_nonce(response)}"' in response.text


def test_csp_nonce_also_supports_the_call_form(tmp_path: Path):
    (tmp_path / "p.html").write_text('<script nonce="{{ csp_nonce() }}">x</script>')
    app = _armed_app()
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/")
    async def index():
        return templates.TemplateResponse("p.html", {})

    response = TestClient(app).get("/")
    assert f'nonce="{_header_nonce(response)}"' in response.text


def test_request_resolves_without_threading_the_request(tmp_path: Path):
    (tmp_path / "p.html").write_text("{{ request.path }}|{{ request.query_params.get('q') }}")
    app = Veloce(openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/here")
    async def here():
        return templates.TemplateResponse("p.html", {})

    assert TestClient(app).get("/here?q=v").text == "/here|v"


def test_explicit_context_still_wins_over_the_resolved_name(tmp_path: Path):
    (tmp_path / "p.html").write_text("{{ request }}")
    app = Veloce(openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/")
    async def index():
        return templates.TemplateResponse("p.html", {"request": "OVERRIDDEN"})

    assert TestClient(app).get("/").text == "OVERRIDDEN"


def test_nonce_is_empty_rather_than_an_error_without_csp(tmp_path: Path):
    """A page must still render when no nonce is armed."""
    (tmp_path / "p.html").write_text('<script nonce="{{ csp_nonce }}">x</script>')
    app = Veloce(openapi_url=None)  # no CSPMiddleware
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/")
    async def index():
        return templates.TemplateResponse("p.html", {})

    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert 'nonce=""' in response.text


def test_nonce_conditional_reflects_whether_one_is_armed(tmp_path: Path):
    (tmp_path / "p.html").write_text("{% if csp_nonce %}armed{% else %}off{% endif %}")
    templates = Jinja2Templates(directory=str(tmp_path))

    armed = _armed_app()

    @armed.get("/")
    async def a():
        return templates.TemplateResponse("p.html", {})

    plain = Veloce(openapi_url=None)

    @plain.get("/")
    async def b():
        return templates.TemplateResponse("p.html", {})

    assert TestClient(armed).get("/").text == "armed"
    assert TestClient(plain).get("/").text == "off"


def test_names_resolve_on_the_streaming_render_path(tmp_path: Path):
    """`stream` renders chunks after the handler returns, so the resolution
    has to survive into the response-consuming task."""
    (tmp_path / "p.html").write_text('<script nonce="{{ csp_nonce }}">{{ request.path }}</script>')
    app = _armed_app()
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/streamed")
    async def streamed():
        return StreamingResponse(templates.stream("p.html", {}), content_type="text/html")

    response = TestClient(app).get("/streamed")
    assert f'nonce="{_header_nonce(response)}"' in response.text
    assert "/streamed" in response.text


def test_names_resolve_on_the_async_render_path(tmp_path: Path):
    (tmp_path / "p.html").write_text('<script nonce="{{ csp_nonce }}">x</script>')
    app = _armed_app()
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/")
    async def index():
        return await templates.render_async("p.html", {})

    response = TestClient(app).get("/")
    assert f'nonce="{_header_nonce(response)}"' in response.text


def test_names_resolve_in_render_template_string_without_a_templates_instance():
    """`render_template_string` falls back to a shared environment when no
    `Jinja2Templates` is bound; the same names must resolve there."""
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware, policy="script-src {nonce}")

    @app.get("/")
    async def index():
        return render_template_string("{{ csp_nonce }}|{{ request.path }}")

    response = TestClient(app).get("/")
    nonce, path = response.text.split("|")
    assert nonce == _header_nonce(response)
    assert path == "/"


def test_rendering_outside_a_request_context_yields_an_empty_nonce():
    app = Veloce(openapi_url=None)
    with app.app_context():
        assert render_template_string("[{{ csp_nonce }}]") == "[]"


def test_each_concurrent_request_renders_its_own_nonce(tmp_path: Path):
    """One environment serves every request, so a request-scoped value that
    leaked into it would hand one client another client's nonce."""
    (tmp_path / "p.html").write_text("{{ request.query_params.get('i') }}|{{ csp_nonce }}")
    app = _armed_app()
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/c")
    async def c():
        await asyncio.sleep(0)  # force the requests to interleave mid-handler
        return templates.TemplateResponse("p.html", {})

    async def drive():
        async with AsyncTestClient(app) as client:
            return await asyncio.gather(*(client.get(f"/c?i={i}") for i in range(50)))

    responses = asyncio.run(drive())
    seen = set()
    for i, response in enumerate(responses):
        index, nonce = response.text.split("|")
        assert index == str(i)
        assert nonce == _header_nonce(response)
        seen.add(nonce)
    assert len(seen) == 50
