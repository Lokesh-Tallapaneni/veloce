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
