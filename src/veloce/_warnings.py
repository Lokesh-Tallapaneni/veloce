"""Warning categories Veloce raises — kept dependency-free.

This module imports nothing from Veloce, so any module can reach it without a
cycle. The public name is exported from the top-level package; leaf modules
own no public surface of their own.
"""

from __future__ import annotations


class VeloceDeprecationWarning(UserWarning):
    """Warns that a Veloce API is deprecated and names what replaces it.

    Rooted at `UserWarning` rather than `DeprecationWarning` on purpose.
    Python's default filter shows a `DeprecationWarning` only when it is raised
    from `__main__`, so a deprecation reached from an application module - which
    is every application served by uvicorn or gunicorn - was silent, and a
    removal promised for a future release would have arrived unannounced.

    Silence it the same way as any other warning category::

        import warnings
        from veloce import VeloceDeprecationWarning

        warnings.filterwarnings("ignore", category=VeloceDeprecationWarning)
    """


class _RegistrationWarning(UserWarning):
    """A diagnostic about a declaration, raised while a route is registered.

    Its own category so an application can silence a declaration diagnostic
    without silencing anything else Veloce or its own code raises - in
    particular `VeloceDeprecationWarning`, which roots at `UserWarning` by
    design and so cannot be told apart from these by category alone.

    Rooted at `UserWarning` so every existing way of filtering these keeps
    working; it is private because filtering the base category still catches it.
    """
