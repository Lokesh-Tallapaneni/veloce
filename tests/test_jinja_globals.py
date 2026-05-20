"""TP8 — url_for / g / current_app available as Jinja globals."""

from __future__ import annotations

from veloce import Veloce, g
from veloce.contrib.templating import Jinja2Templates


def _bind(app: Veloce) -> Any:  # type: ignore[name-defined]
    from veloce.helpers import _current_app_var

    return _current_app_var.set(app)


def _unbind(token: Any) -> None:  # type: ignore[name-defined]
    from veloce.helpers import _current_app_var

    _current_app_var.reset(token)


def test_url_for_available_in_template(tmp_path):
    app = Veloce(openapi_url=None)

    @app.get("/users/{id}", name="user_detail")
    async def user_detail(id: str):
        return {}

    templates = Jinja2Templates(directory=str(tmp_path))
    app._templates = templates

    token = _bind(app)
    try:
        out = templates.render_string(
            "{{ url_for('user_detail', id='42') }}",
            {},
        )
        assert out == "/users/42"
    finally:
        _unbind(token)


def test_g_available_in_template(tmp_path):
    app = Veloce(openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))
    app._templates = templates

    token = _bind(app)
    try:
        # Set something on g, render a template that reads it.
        g._reset()
        g.user = "alice"
        out = templates.render_string("{{ g.user }}", {})
        assert out == "alice"
    finally:
        _unbind(token)


def test_current_app_attribute_in_template(tmp_path):
    app = Veloce(title="MyApp", openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))
    app._templates = templates

    token = _bind(app)
    try:
        out = templates.render_string("{{ current_app.title }}", {})
        assert out == "MyApp"
    finally:
        _unbind(token)


# Pull `Any` in for the type-checker
from typing import Any  # noqa: E402
