"""Jinja templating registry — filters, globals, tests, context processors.

Mixed into `Veloce`. Registration-time methods that populate the app's Jinja
environment and context-processor list, so they sit off the per-request path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any


class TemplatingMixin:
    """Register Jinja filters, globals, tests, and context processors."""

    if TYPE_CHECKING:  # pragma: no cover
        # Attributes the host application (`Veloce`) provides.
        _template_filters: Any
        _template_globals: Any
        _template_tests: Any
        _context_processors: Any
        _assert_mutable: Callable[..., Any]

    def context_processor(self, func: Callable) -> Callable:
        """Register a template context processor.

        The function should return a dict that merges into the template
        context.
        """
        self._assert_mutable()
        self._context_processors.append(func)
        return func

    # ── Jinja2 helper registration ────────────────────────

    def template_filter(self, name: str | None = None) -> Callable:
        """Register a function as a Jinja filter.

        Usage::

            @app.template_filter("upper")
            def upper(s): return s.upper()

        The filter becomes available in every `Jinja2Templates` render that
        runs inside this app's request scope. `name` defaults to the
        function's own `__name__`.
        """

        def decorator(func: Callable) -> Callable:
            filter_name = name or func.__name__
            self._template_filters.append((filter_name, func))
            return func

        return decorator

    def template_global(self, name: str | None = None) -> Callable:
        """Register a callable as a Jinja global, reachable by name in a template.

        Same shape as `template_filter`.
        """

        def decorator(func: Callable) -> Callable:
            global_name = name or func.__name__
            self._template_globals.append((global_name, func))
            return func

        return decorator

    def add_template_global(self, func: Callable, name: str | None = None) -> None:
        """Imperative equivalent of `@template_global`."""
        self._template_globals.append((name or func.__name__, func))

    def template_test(self, name: str | None = None) -> Callable:
        """Register a Jinja test - used in `{% if x is name %}` constructs."""

        def decorator(func: Callable) -> Callable:
            test_name = name or func.__name__
            self._template_tests.append((test_name, func))
            return func

        return decorator

    def add_template_filter(self, func: Callable, name: str | None = None) -> None:
        """Imperative equivalent of `@template_filter`."""
        self._template_filters.append((name or func.__name__, func))

    def add_template_test(self, func: Callable, name: str | None = None) -> None:
        """Imperative equivalent of `@template_test`."""
        self._template_tests.append((name or func.__name__, func))

    def update_template_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """Merge registered context-processor output into `context`.

        Runs every `@app.context_processor` callback and folds the
        returned dicts into `context` **in place**, without overriding
        keys the caller already set (the documented semantics - explicit context
        wins). Returns the same dict for chaining.
        """
        for processor in self._context_processors:
            result = processor()
            if isinstance(result, dict):
                for k, v in result.items():
                    context.setdefault(k, v)
        return context
