"""app.add_template_filter / add_template_test, observed through a render.

Every test here used to assert that a `(name, function)` tuple had been appended
to `app._template_filters` or `app._template_tests`. That is the registry, not
the behaviour: delete the Jinja sync step that copies those lists into
`env.filters` / `env.tests` - the only part a user can observe - and all six
still passed. These two public helpers had no behavioural coverage anywhere in
the suite.

They render now, in the same shape `test_template_helpers.py` uses for the
decorator forms, so the assertions survive the registry changing shape and fail
if the name never reaches the template environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veloce import Veloce
from veloce.contrib.templating import Jinja2Templates
from veloce.testclient import TestClient


@pytest.fixture
def tmpl_dir(tmp_path: Path) -> Path:
    return tmp_path


def _render(app: Veloce, directory: Path, template: str, context: dict) -> bytes:
    """Serve `template` from `directory` through the app and return the body."""
    templates = Jinja2Templates(directory=str(directory))

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", context)

    return TestClient(app).get("/").body


# ── add_template_filter ──────────────────────────────────────────────


def test_add_template_filter_uses_function_name_by_default(tmpl_dir: Path):
    (tmpl_dir / "x.html").write_text("{{ name|shout }}")
    app = Veloce(debug=True, openapi_url=None)

    def shout(s: str) -> str:
        return s.upper()

    app.add_template_filter(shout)
    assert _render(app, tmpl_dir, "x.html", {"name": "alice"}) == b"ALICE"


def test_add_template_filter_accepts_explicit_name(tmpl_dir: Path):
    (tmpl_dir / "x.html").write_text("{{ name|loud }}")
    app = Veloce(debug=True, openapi_url=None)

    def shout(s: str) -> str:
        return s.upper()

    app.add_template_filter(shout, "loud")
    assert _render(app, tmpl_dir, "x.html", {"name": "alice"}) == b"ALICE"


def test_add_template_filter_multiple_distinct_entries(tmpl_dir: Path):
    """Two filters registered on one app both resolve, under both names."""
    (tmpl_dir / "x.html").write_text("{{ s|a }}-{{ s|b_alias }}")
    app = Veloce(debug=True, openapi_url=None)

    def a(x: str) -> str:
        return x + "1"

    def b(x: str) -> str:
        return x + "2"

    app.add_template_filter(a)
    app.add_template_filter(b, "b_alias")
    assert _render(app, tmpl_dir, "x.html", {"s": "v"}) == b"v1-v2"


# ── add_template_test ────────────────────────────────────────────────


def test_add_template_test_uses_function_name_by_default(tmpl_dir: Path):
    (tmpl_dir / "x.html").write_text("{% if n is positive %}yes{% else %}no{% endif %}")
    app = Veloce(debug=True, openapi_url=None)

    def positive(x: int) -> bool:
        return x > 0

    app.add_template_test(positive)
    assert _render(app, tmpl_dir, "x.html", {"n": 3}) == b"yes"


def test_add_template_test_accepts_explicit_name(tmpl_dir: Path):
    (tmpl_dir / "x.html").write_text("{% if n is pos %}yes{% else %}no{% endif %}")
    app = Veloce(debug=True, openapi_url=None)

    def positive(x: int) -> bool:
        return x > 0

    app.add_template_test(positive, "pos")
    assert _render(app, tmpl_dir, "x.html", {"n": -1}) == b"no"


# ── the two registration forms together ──────────────────────────────


def test_decorator_and_imperative_coexist(tmpl_dir: Path):
    """Registering one of each must not drop either from the environment."""
    (tmpl_dir / "x.html").write_text("{{ s|upper_dec }}-{{ s|lower_imp }}")
    app = Veloce(debug=True, openapi_url=None)

    @app.template_filter("upper_dec")
    def upper(s: str) -> str:
        return s.upper()

    def lower(s: str) -> str:
        return s.lower()

    app.add_template_filter(lower, "lower_imp")
    assert _render(app, tmpl_dir, "x.html", {"s": "Ab"}) == b"AB-ab"
