"""app.add_template_filter / add_template_test."""

from __future__ import annotations

from veloce import Veloce


def test_add_template_filter_uses_function_name_by_default():
    app = Veloce()

    def shout(s):
        return s.upper()

    app.add_template_filter(shout)
    assert ("shout", shout) in app._template_filters


def test_add_template_filter_accepts_explicit_name():
    app = Veloce()

    def shout(s):
        return s.upper()

    app.add_template_filter(shout, "loud")
    assert ("loud", shout) in app._template_filters


def test_add_template_filter_multiple_distinct_entries():
    app = Veloce()

    def a(x):
        return x

    def b(x):
        return x

    app.add_template_filter(a)
    app.add_template_filter(b, "b_alias")
    names = [n for n, _ in app._template_filters]
    assert "a" in names
    assert "b_alias" in names


def test_add_template_test_uses_function_name_by_default():
    app = Veloce()

    def positive(x):
        return x > 0

    app.add_template_test(positive)
    assert ("positive", positive) in app._template_tests


def test_add_template_test_accepts_explicit_name():
    app = Veloce()

    def positive(x):
        return x > 0

    app.add_template_test(positive, "pos")
    assert ("pos", positive) in app._template_tests


def test_decorator_and_imperative_coexist():
    app = Veloce()

    @app.template_filter("upper_dec")
    def upper(s):
        return s.upper()

    def lower(s):
        return s.lower()

    app.add_template_filter(lower, "lower_imp")
    names = [n for n, _ in app._template_filters]
    assert "upper_dec" in names
    assert "lower_imp" in names
