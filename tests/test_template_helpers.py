"""Jinja filter/global/test decorator tests (TP5)."""

from __future__ import annotations

from pathlib import Path

from veloce import Veloce
from veloce.contrib.templating import Jinja2Templates, render_template_string
from veloce.testclient import TestClient

# ── @template_filter ───────────────────────────────────────────────────


def test_template_filter_registered_by_function_name(tmp_path: Path):
    (tmp_path / "x.html").write_text("{{ name|shout }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.template_filter()
    def shout(s: str) -> str:
        return s.upper() + "!"

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {"name": "alice"})

    resp = TestClient(app).get("/")
    assert resp.body == b"ALICE!"


def test_template_filter_explicit_name(tmp_path: Path):
    (tmp_path / "x.html").write_text("{{ s|reversed_str }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.template_filter("reversed_str")
    def reverse_helper(s: str) -> str:
        return s[::-1]

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {"s": "hello"})

    resp = TestClient(app).get("/")
    assert resp.body == b"olleh"


# ── @template_global ───────────────────────────────────────────────────


def test_template_global_callable_in_templates(tmp_path: Path):
    (tmp_path / "x.html").write_text("{{ now() }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.template_global()
    def now() -> str:
        return "<TIME>"

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {})

    resp = TestClient(app).get("/")
    # `.html` triggers autoescape (Jinja `select_autoescape` default) so
    # the global callable's `<TIME>` return is HTML-escaped.
    assert resp.body == b"&lt;TIME&gt;"


def test_add_template_global_imperative(tmp_path: Path):
    (tmp_path / "x.html").write_text("{{ greet('alice') }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    def greet(who: str) -> str:
        return f"Hi {who}"

    app.add_template_global(greet)

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {})

    resp = TestClient(app).get("/")
    assert resp.body == b"Hi alice"


# ── @template_test ─────────────────────────────────────────────────────


def test_template_test_usable_in_if_expressions(tmp_path: Path):
    (tmp_path / "x.html").write_text("{% if n is even %}yes{% else %}no{% endif %}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.template_test("even")
    def is_even(n: int) -> bool:
        return n % 2 == 0

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {"n": 4})

    resp = TestClient(app).get("/")
    assert resp.body == b"yes"


# ── Idempotency / interaction with context_processor ──────────────────


def test_filter_and_context_processor_coexist(tmp_path: Path):
    (tmp_path / "x.html").write_text("{{ name|upper_full }} from {{ brand }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.template_filter()
    def upper_full(s: str) -> str:
        return s.upper()

    @app.context_processor
    def brand_proc():
        return {"brand": "Veloce"}

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {"name": "alice"})

    resp = TestClient(app).get("/")
    assert resp.body == b"ALICE from Veloce"


def test_multiple_renders_sync_idempotently(tmp_path: Path):
    """Calling render twice doesn't multiply or break filter registration."""
    (tmp_path / "x.html").write_text("{{ x|tag }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.template_filter()
    def tag(s: str) -> str:
        return f"[{s}]"

    @app.get("/x")
    async def index():
        return templates.TemplateResponse("x.html", {"x": "ok"})

    client = TestClient(app)
    r1 = client.get("/x")
    r2 = client.get("/x")
    assert r1.body == r2.body == b"[ok]"


# ── The string helper sees app-registered helpers ────────────────────


def _string_app(suffix: str = "") -> Veloce:
    """An app with no template folder, so the string helper takes its fallback."""
    app = Veloce(openapi_url=None)

    @app.template_filter("shout")
    def shout(value):  # noqa: ANN001, ANN202
        return str(value).upper() + suffix

    @app.template_global()
    def house():  # noqa: ANN202
        return "nordwind" + suffix

    @app.template_test("loud")
    def loud(value):  # noqa: ANN001, ANN202
        return str(value).isupper()

    return app


def test_a_registered_filter_resolves_without_a_template_folder():
    """The fallback environment never received the app's helpers.

    `render_template_string` fell back to a bare environment, so a template
    using `@app.template_filter` raised `No filter named ...` and surfaced as
    a 500 with nothing pointing at the cause - while the docstring said the
    helper honours app-level filters.
    """
    app = _string_app()

    @app.get("/")
    async def index():
        return render_template_string("{{ x|shout }}", x="hi")

    with TestClient(app) as client:
        assert client.get("/").text == "HI"


def test_a_registered_global_resolves_too():
    app = _string_app()

    @app.get("/")
    async def index():
        return render_template_string("{{ house() }}")

    with TestClient(app) as client:
        assert client.get("/").text == "nordwind"


def test_a_registered_test_resolves_too():
    app = _string_app()

    @app.get("/")
    async def index():
        return render_template_string("{{ 'AB' is loud }}")

    with TestClient(app) as client:
        assert client.get("/").text == "True"


def test_one_app_does_not_see_another_app_filter():
    """The fallback env is per app, so registrations cannot bleed across."""
    first, second = _string_app("!"), _string_app("?")

    for app in (first, second):

        @app.get("/")
        async def index():
            return render_template_string("{{ x|shout }}", x="hi")

    with TestClient(first) as client:
        assert client.get("/").text == "HI!"
    with TestClient(second) as client:
        assert client.get("/").text == "HI?"


def test_a_filter_registered_after_the_first_render_is_picked_up():
    """The sync is token-keyed on the registration counts, not once-only."""
    app = _string_app()

    @app.get("/")
    async def index():
        return render_template_string("{{ x|whisper }}", x="HI")

    with TestClient(app) as client:
        assert client.get("/").status_code == 500

        @app.template_filter("whisper")
        def whisper(value):  # noqa: ANN001, ANN202
            return str(value).lower()

        assert client.get("/").text == "hi"


def test_an_unregistered_filter_still_fails():
    """Syncing the app's helpers must not make an unknown name resolve."""
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return render_template_string("{{ x|nosuchfilter }}", x="hi")

    with TestClient(app) as client:
        assert client.get("/").status_code == 500
