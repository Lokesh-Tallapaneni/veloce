"""app.app_context() and app.test_request_context() (D12)."""

from __future__ import annotations

import pytest

from veloce import Veloce, current_app, g

# ── app_context ──────────────────────────────────────────────────────


def test_current_app_unbound_outside_context_raises():
    with pytest.raises(RuntimeError, match="Working outside"):
        _ = current_app.title


def test_app_context_binds_current_app():
    app = Veloce(title="Demo", openapi_url=None)
    with app.app_context():
        assert current_app.title == "Demo"


def test_app_context_unbinds_on_exit():
    app = Veloce(title="Demo", openapi_url=None)
    with app.app_context():
        assert bool(current_app) is True
    assert bool(current_app) is False


def test_app_context_nested_restores_previous():
    """Nested binding: outer app rebound when inner exits."""
    a = Veloce(title="A", openapi_url=None)
    b = Veloce(title="B", openapi_url=None)
    with a.app_context():
        assert current_app.title == "A"
        with b.app_context():
            assert current_app.title == "B"
        assert current_app.title == "A"


def test_app_context_g_is_isolated_per_block():
    """Two consecutive blocks see fresh `g` (not leaked from the prior block)."""
    app = Veloce(openapi_url=None)
    with app.app_context():
        g.user = "alice"
    with app.app_context():
        assert "user" not in g


# ── test_request_context ─────────────────────────────────────────────


def test_test_request_context_binds_request_and_app():
    app = Veloce(title="X", openapi_url=None)
    with app.test_request_context(path="/foo", method="POST", query_string="x=1") as req:
        assert req.path == "/foo"
        assert req.method == "POST"
        assert req.query_string == "x=1"
        assert current_app.title == "X"


def test_test_request_context_resets_on_exit():
    app = Veloce(openapi_url=None)
    with app.test_request_context():
        assert bool(current_app) is True
    assert bool(current_app) is False


def test_test_request_context_makes_g_writable():
    """Inside the block g acts like during a real request."""
    app = Veloce(openapi_url=None)
    with app.test_request_context():
        g.x = 7
        assert g.x == 7
    # After exit g is a fresh store again.
    with app.test_request_context():
        assert "x" not in g


def test_test_request_context_headers_propagate():
    app = Veloce(openapi_url=None)
    with app.test_request_context(headers={"Accept": "application/json"}) as req:
        assert req.headers.get("accept") == "application/json"
