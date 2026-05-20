"""app.blueprints / app.iter_blueprints / has_*_context."""

from __future__ import annotations

from veloce import Veloce, has_app_context, has_request_context
from veloce.blueprints import Blueprint

# ── app.blueprints / iter_blueprints ────────────────────────────────


def test_blueprints_empty_on_fresh_app():
    app = Veloce()
    assert app.blueprints == {}
    assert list(app.iter_blueprints()) == []


def test_blueprints_records_registered():
    app = Veloce()
    api = Blueprint("api", url_prefix="/api")
    admin = Blueprint("admin", url_prefix="/admin")
    app.register_blueprint(api)
    app.register_blueprint(admin)

    assert set(app.blueprints.keys()) == {"api", "admin"}
    assert app.blueprints["api"] is api
    assert app.blueprints["admin"] is admin


def test_iter_blueprints_preserves_registration_order():
    app = Veloce()
    first = Blueprint("first")
    second = Blueprint("second")
    third = Blueprint("third")
    app.register_blueprint(first)
    app.register_blueprint(second)
    app.register_blueprint(third)

    assert list(app.iter_blueprints()) == [first, second, third]


def test_blueprints_returns_snapshot_copy():
    app = Veloce()
    api = Blueprint("api")
    app.register_blueprint(api)

    snap = app.blueprints
    snap["fake"] = object()
    # Original state untouched.
    assert "fake" not in app.blueprints
    assert list(app.blueprints.keys()) == ["api"]


def test_re_registering_same_name_overwrites():
    """the documented semantics: registering a second blueprint under the same
    name replaces the previous entry in `app.blueprints`."""
    app = Veloce()
    v1 = Blueprint("api")
    v2 = Blueprint("api")
    app.register_blueprint(v1)
    app.register_blueprint(v2)

    assert app.blueprints["api"] is v2


# ── has_app_context / has_request_context ───────────────────────────


def test_has_app_context_false_outside_request():
    assert has_app_context() is False


def test_has_request_context_false_outside_request():
    assert has_request_context() is False


def test_has_app_context_true_inside_app_context():
    app = Veloce()
    with app.app_context():
        assert has_app_context() is True
    # And False once exited.
    assert has_app_context() is False


def test_has_request_context_true_inside_test_request_context():
    app = Veloce()
    with app.test_request_context():
        assert has_request_context() is True
    assert has_request_context() is False
