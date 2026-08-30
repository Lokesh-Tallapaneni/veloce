"""TP8 — url_for / g / current_app available as Jinja globals."""

from __future__ import annotations

from tests._templating import install_templates
from veloce import Veloce, g
from veloce.contrib.templating import Jinja2Templates


def test_url_for_available_in_template(tmp_path):
    app = Veloce(openapi_url=None)

    @app.get("/users/{id}", name="user_detail")
    async def user_detail(id: str):
        return {}

    templates = Jinja2Templates(directory=str(tmp_path))
    install_templates(app, templates)

    with app.app_context():
        out = templates.render_string(
            "{{ url_for('user_detail', id='42') }}",
            {},
        )
        assert out == "/users/42"


def test_g_available_in_template(tmp_path):
    app = Veloce(openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))
    install_templates(app, templates)

    with app.app_context():
        # Set something on g, render a template that reads it.
        g._reset()
        g.user = "alice"
        out = templates.render_string("{{ g.user }}", {})
        assert out == "alice"


def test_current_app_attribute_in_template(tmp_path):
    app = Veloce(title="MyApp", openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))
    install_templates(app, templates)

    with app.app_context():
        out = templates.render_string("{{ current_app.title }}", {})
        assert out == "MyApp"


def test_get_flashed_messages_available_in_template(tmp_path):
    """A template may call `get_flashed_messages()` without manual registration.

    Outside a request the helper returns an empty list, so the render must
    succeed rather than raising `jinja2.UndefinedError` for an unknown global.
    """
    app = Veloce(openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))
    install_templates(app, templates)

    with app.app_context():
        out = templates.render_string(
            "{% for m in get_flashed_messages() %}{{ m }}{% endfor %}OK", {}
        )
        assert out == "OK"
