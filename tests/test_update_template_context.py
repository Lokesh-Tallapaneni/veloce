"""app.update_template_context context population."""

from __future__ import annotations

from veloce import Veloce


def test_empty_context_with_no_processors():
    app = Veloce()
    ctx = {"page": "home"}
    out = app.update_template_context(ctx)
    assert out == {"page": "home"}
    assert out is ctx


def test_context_processor_output_merged():
    app = Veloce()

    @app.context_processor
    def inject():
        return {"site_name": "Veloce", "year": 2026}

    ctx: dict = {}
    app.update_template_context(ctx)
    assert ctx == {"site_name": "Veloce", "year": 2026}


def test_explicit_context_wins_over_processor():
    app = Veloce()

    @app.context_processor
    def inject():
        return {"title": "default"}

    ctx = {"title": "explicit"}
    app.update_template_context(ctx)
    # Caller's value is not overridden.
    assert ctx["title"] == "explicit"


def test_multiple_processors_all_applied():
    app = Veloce()

    @app.context_processor
    def a():
        return {"a": 1}

    @app.context_processor
    def b():
        return {"b": 2}

    ctx: dict = {}
    app.update_template_context(ctx)
    assert ctx == {"a": 1, "b": 2}


def test_returns_same_dict_for_chaining():
    app = Veloce()
    ctx: dict = {"x": 1}
    assert app.update_template_context(ctx) is ctx
