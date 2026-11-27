"""`app.middlewares` - reading the pipeline without opening `_middlewares`.

Whether a middleware is installed is a public-facing question: a plugin that
must not register itself twice, a startup check that CORS is present, a
diagnostic that prints the pipeline. There was no way to ask it, so eighteen
sites across eight test modules read `app._middlewares` instead - and so would
any out-of-tree code with the same question.

`middlewares` returns a tuple, so the answer cannot be used to mutate the
pipeline behind `add_middleware`'s back: the priority ordering, the generation
counter and the feature-state registration all hang off that entry point.
"""

from __future__ import annotations

from veloce import Veloce
from veloce.middleware import Middleware


class _A(Middleware):
    pass


class _B(Middleware):
    pass


class _C(Middleware):
    pass


# ── what it contains ─────────────────────────────────────────────────


def test_a_new_app_has_no_middleware():
    assert Veloce(openapi_url=None).middlewares == ()


def test_a_registered_middleware_appears():
    app = Veloce(openapi_url=None)
    app.add_middleware(_A)
    assert len(app.middlewares) == 1
    assert isinstance(app.middlewares[0], _A)


def test_an_instance_is_the_object_registered():
    """Not a copy - `is`, so a caller can find the one it installed."""
    app = Veloce(openapi_url=None)
    instance = _A()
    app.add_middleware(instance)
    assert app.middlewares[-1] is instance


def test_registration_order_is_preserved():
    app = Veloce(openapi_url=None)
    app.add_middleware(_A)
    app.add_middleware(_B)
    app.add_middleware(_C)
    assert [type(m) for m in app.middlewares] == [_A, _B, _C]


def test_priorities_are_reflected():
    """With priorities set, this is the order the pipeline runs, not the order
    the calls were made. Higher priority runs earlier in the request phase."""
    app = Veloce(openapi_url=None)
    app.add_middleware(_A, priority=10)
    app.add_middleware(_B, priority=50)
    assert [type(m) for m in app.middlewares] == [_B, _A]


def test_equal_priorities_keep_registration_order():
    app = Veloce(openapi_url=None)
    app.add_middleware(_A, priority=5)
    app.add_middleware(_B, priority=5)
    assert [type(m) for m in app.middlewares] == [_A, _B]


def test_the_view_matches_the_pipeline_it_describes():
    app = Veloce(openapi_url=None)
    app.add_middleware(_A)
    app.add_middleware(_B)
    assert list(app.middlewares) == app._middlewares


# ── it is a snapshot, and read-only ──────────────────────────────────


def test_it_is_a_tuple():
    """Mutating the list would bypass the ordering and versioning that hang off
    `add_middleware`."""
    assert isinstance(Veloce(openapi_url=None).middlewares, tuple)


def test_a_held_snapshot_does_not_change_underneath():
    app = Veloce(openapi_url=None)
    app.add_middleware(_A)
    snapshot = app.middlewares
    app.add_middleware(_B)
    assert len(snapshot) == 1
    assert len(app.middlewares) == 2


def test_two_apps_keep_separate_pipelines():
    first, second = Veloce(openapi_url=None), Veloce(openapi_url=None)
    first.add_middleware(_A)
    assert len(first.middlewares) == 1
    assert second.middlewares == ()


# ── the question it exists to answer ─────────────────────────────────


def test_an_installed_middleware_can_be_found_by_type():
    app = Veloce(openapi_url=None)
    app.add_middleware(_A)
    assert any(isinstance(m, _A) for m in app.middlewares)
    assert not any(isinstance(m, _B) for m in app.middlewares)


def test_registering_the_same_class_twice_shows_twice():
    """The negative behind the duplicate-guard use: the view does not dedupe,
    so a guard built on it actually has something to guard against."""
    app = Veloce(openapi_url=None)
    app.add_middleware(_A)
    app.add_middleware(_A)
    assert len(app.middlewares) == 2
