"""Headers.to_wsgi_list / copy / add methods."""

from __future__ import annotations

from veloce.http.datastructures import Headers

# ── to_wsgi_list ────────────────────────────────────────────────────


def test_to_wsgi_list_returns_tuples():
    h = Headers({"Content-Type": "text/html", "X-Custom": "v"})
    wl = h.to_wsgi_list()
    assert isinstance(wl, list)
    assert ("Content-Type", "text/html") in wl
    assert ("X-Custom", "v") in wl


def test_to_wsgi_list_preserves_duplicates():
    h = Headers()
    h.add("Set-Cookie", "a=1")
    h.add("Set-Cookie", "b=2")
    cookies = [v for k, v in h.to_wsgi_list() if k.lower() == "set-cookie"]
    assert cookies == ["a=1", "b=2"]


# ── copy ────────────────────────────────────────────────────────────


def test_copy_is_independent():
    h = Headers({"X": "1"})
    c = h.copy()
    c["X"] = "changed"
    assert h["X"] == "1"
    assert c["X"] == "changed"


def test_copy_is_headers_instance():
    assert isinstance(Headers({"a": "b"}).copy(), Headers)


# ── add with parameters ─────────────────────────────────────────────


def test_add_plain_value():
    h = Headers()
    h.add("X-Test", "value")
    assert h["X-Test"] == "value"


def test_add_with_parameters():
    h = Headers()
    h.add("Content-Disposition", "attachment", filename="report.pdf")
    assert h["Content-Disposition"] == "attachment; filename=report.pdf"


def test_add_quotes_parameter_with_spaces():
    h = Headers()
    h.add("Content-Disposition", "attachment", filename="my file.txt")
    assert 'filename="my file.txt"' in h["Content-Disposition"]


def test_add_underscore_param_becomes_hyphen():
    h = Headers()
    h.add("X-H", "v", access_control="yes")
    assert "access-control=yes" in h["X-H"]
