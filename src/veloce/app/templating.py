"""Jinja templating registry — filters, globals, tests, context processors.

Mixed into `Veloce`. The `jinja_env` / `jinja_loader` accessors plus the
registration-time methods that populate them, so they sit off the request path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from veloce._internal import T_route
from veloce.app._host import AppHost


class TemplatingMixin(AppHost):
    """Register Jinja filters, globals, tests, and context processors."""

    # ── The bound Jinja environment ───────────────────────

    @property
    def jinja_env(self) -> Any:
        """The app's shared Jinja2 `Environment`, mutable in place.

        Available once a `template_folder` has been configured; raises
        `RuntimeError` when no templating is.
        """
        if self._templates is None:
            raise RuntimeError(
                "no Jinja environment - pass `template_folder=` to Veloce(...) "
                "or bind a Jinja2Templates instance first"
            )
        return self._templates.env

    @property
    def jinja_loader(self) -> Any:
        """The app's Jinja template loader, or `None` when no templating is set up."""
        if self._templates is None:
            return None
        return self._templates.env.loader

    def context_processor(self, func: T_route) -> T_route:
        """Register a context processor returning a dict to merge into the context."""
        self._assert_mutable()
        self._context_processors.append(func)
        return func

    # ── Jinja2 helper registration ────────────────────────

    def template_filter(self, name: str | None = None) -> Callable[[T_route], T_route]:
        """Register a function as a Jinja filter.

        Usage::

            @app.template_filter("upper")
            def upper(s): return s.upper()

        `name` defaults to the function's own `__name__`.
        """

        def decorator(func: T_route) -> T_route:
            filter_name = name or func.__name__
            self._template_filters.append((filter_name, func))
            return func

        return decorator

    def template_global(self, name: str | None = None) -> Callable[[T_route], T_route]:
        """Register a callable as a Jinja global, reachable by name in a template.

        Same shape as `template_filter`.
        """

        def decorator(func: T_route) -> T_route:
            global_name = name or func.__name__
            self._template_globals.append((global_name, func))
            return func

        return decorator

    def add_template_global(self, func: Callable[..., Any], name: str | None = None) -> None:
        """Imperative equivalent of `@template_global`."""
        self._template_globals.append((name or func.__name__, func))

    def template_test(self, name: str | None = None) -> Callable[[T_route], T_route]:
        """Register a Jinja test - used in `{% if x is name %}` constructs."""

        def decorator(func: T_route) -> T_route:
            test_name = name or func.__name__
            self._template_tests.append((test_name, func))
            return func

        return decorator

    def add_template_filter(self, func: Callable[..., Any], name: str | None = None) -> None:
        """Imperative equivalent of `@template_filter`."""
        self._template_filters.append((name or func.__name__, func))

    def add_template_test(self, func: Callable[..., Any], name: str | None = None) -> None:
        """Imperative equivalent of `@template_test`."""
        self._template_tests.append((name or func.__name__, func))

    def update_template_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """Merge registered context-processor output into `context`.

        Folds the returned dicts in **in place** without overriding keys the
        caller already set - explicit context wins - and returns the same dict.
        """
        for processor in self._context_processors:
            result = processor()
            if isinstance(result, dict):
                for k, v in result.items():
                    context.setdefault(k, v)
        return context
