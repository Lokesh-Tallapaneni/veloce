"""Markup + escape — HTML-safety primitives."""

from __future__ import annotations

from veloce import Markup, escape

# ── escape ───────────────────────────────────────────────────────────


def test_escape_replaces_five_html_chars():
    assert escape('<a href="x">&amp;\'</a>') == (
        "&lt;a href=&#34;x&#34;&gt;&amp;amp;&#39;&lt;/a&gt;"
    )


def test_escape_returns_markup_instance():
    out = escape("safe text")
    assert isinstance(out, Markup)
    assert isinstance(out, str)


def test_escape_str_coerces_non_string():
    assert escape(42) == "42"
    assert escape(None) == "None"


def test_escape_respects_html_method():
    class Custom:
        def __html__(self):
            return "<safe/>"

    out = escape(Custom())
    assert out == "<safe/>"
    assert isinstance(out, Markup)


def test_escape_passes_markup_through_unchanged():
    pre = Markup("<b>bold</b>")
    out = escape(pre)
    assert out == "<b>bold</b>"


# ── Markup ──────────────────────────────────────────────────────────


def test_markup_is_str_subclass():
    m = Markup("<b>x</b>")
    assert isinstance(m, str)
    assert m == "<b>x</b>"


def test_markup_html_method_returns_self():
    m = Markup("<b>x</b>")
    assert m.__html__() == "<b>x</b>"


def test_markup_concat_escapes_plain_string():
    out = Markup("<b>") + "<x>" + Markup("</b>")
    # Plain `<x>` got escaped during concatenation; Markup tags survived.
    assert out == "<b>&lt;x&gt;</b>"
    assert isinstance(out, Markup)


def test_markup_radd_escapes_plain_left_operand():
    out = "<unsafe>" + Markup("<safe>")
    assert out == "&lt;unsafe&gt;<safe>"
    assert isinstance(out, Markup)


def test_markup_percent_format_escapes_arguments():
    template = Markup("<p>%s</p>")
    out = template % "<bad>"
    assert out == "<p>&lt;bad&gt;</p>"
    assert isinstance(out, Markup)


def test_markup_percent_format_tuple_args():
    out = Markup("<a>%s</a><b>%s</b>") % ("<x>", "<y>")
    assert out == "<a>&lt;x&gt;</a><b>&lt;y&gt;</b>"


def test_markup_wraps_html_object():
    class Custom:
        def __html__(self):
            return "<custom/>"

    m = Markup(Custom())
    assert m == "<custom/>"


def test_markup_repr_distinguishes_from_str():
    assert repr(Markup("x")) == "Markup('x')"
