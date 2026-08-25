"""app.url_value_preprocessors / app.url_default_functions inspection."""

from __future__ import annotations

from veloce import Veloce


def test_url_value_preprocessors_starts_empty():
    app = Veloce()
    assert app.url_value_preprocessors == {None: []}


def test_url_value_preprocessors_lists_registered():
    app = Veloce()

    @app.url_value_preprocessor
    def pull_lang(endpoint, values):
        pass

    spec = app.url_value_preprocessors
    assert list(spec.keys()) == [None]
    assert pull_lang in spec[None]


def test_url_value_preprocessors_returns_snapshot():
    """Mutating the returned dict doesn't affect framework state."""
    app = Veloce()

    @app.url_value_preprocessor
    def pull_lang(endpoint, values):
        pass

    spec = app.url_value_preprocessors
    spec[None].clear()
    spec["bp"] = []
    # Original state untouched.
    assert pull_lang in app.url_value_preprocessors[None]
    assert list(app.url_value_preprocessors.keys()) == [None]


def test_url_default_functions_starts_empty():
    app = Veloce()
    assert app.url_default_functions == {None: []}


def test_url_default_functions_lists_registered():
    app = Veloce()

    @app.url_defaults
    def add_lang(endpoint, values):
        values.setdefault("lang", "en")

    spec = app.url_default_functions
    assert add_lang in spec[None]


def test_url_default_functions_returns_snapshot():
    app = Veloce()

    @app.url_defaults
    def add_lang(endpoint, values):
        pass

    spec = app.url_default_functions
    spec[None].clear()
    assert add_lang in app.url_default_functions[None]


def test_url_processors_collected_from_blueprints():
    """Blueprint-registered processors are bucketed under the blueprint's name."""
    from veloce.blueprints import Blueprint

    app = Veloce()
    bp = Blueprint("api", url_prefix="/api")

    @bp.url_value_preprocessor
    def pull_v(endpoint, values):
        pass

    @bp.url_defaults
    def add_v(endpoint, values):
        pass

    app.register_blueprint(bp)
    # Under the blueprint's name, unwrapped - the app's own list stays empty.
    assert app.url_value_preprocessors[None] == []
    assert app.url_default_functions[None] == []
    assert app.url_value_preprocessors["api"] == [pull_v]
    assert app.url_default_functions["api"] == [add_v]
