"""Veloce.error_handler_spec + before/after/teardown_request_funcs."""

from __future__ import annotations

from veloce import Veloce


def test_error_handler_spec_empty_by_default():
    app = Veloce(openapi_url=None)
    assert app.error_handler_spec == {None: {}}


def test_error_handler_spec_includes_status_and_class_handlers():
    app = Veloce(openapi_url=None)

    @app.errorhandler(404)
    async def nf(request, exc):
        return {}

    @app.errorhandler(ValueError)
    async def ve(request, exc):
        return {}

    spec = app.error_handler_spec
    assert 404 in spec[None]
    assert ValueError in spec[None]


def test_before_request_funcs_lists_registered_hooks():
    app = Veloce(openapi_url=None)
    hooks = []

    @app.before_request
    def a(request):
        hooks.append("a")

    @app.before_request
    def b(request):
        hooks.append("b")

    funcs = app.before_request_funcs[None]
    assert len(funcs) == 2


def test_after_request_funcs_independent_of_teardown():
    app = Veloce(openapi_url=None)

    @app.after_request
    def a(request, response):
        return response

    @app.teardown_request
    def t(exc):
        pass

    assert len(app.after_request_funcs[None]) == 1
    assert len(app.teardown_request_funcs[None]) == 1


def test_view_dicts_are_snapshots_not_live():
    """Mutating the returned list shouldn't bleed into the app state."""
    app = Veloce(openapi_url=None)

    @app.before_request
    def a(request):
        pass

    funcs = app.before_request_funcs[None]
    funcs.append(lambda r: None)
    # The app's internal list is unchanged.
    assert len(app._before_request_hooks) == 1
