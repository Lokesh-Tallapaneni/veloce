"""Jinja2 templating — lazy import, zero cost when unused."""

from __future__ import annotations

import inspect
from typing import Any

from veloce.http.response import HTMLResponse


def _sync_app_jinja_helpers(env: Any) -> None:
    """Copy filters/globals/tests registered on the active app into `env`.

    Also injects the standard template globals — `url_for`, `g`, and
    `current_app` — so templates can `{{ url_for('endpoint') }}` and
    `{{ g.user }}` without manual context plumbing. `request` is bound
    by `_gather_context_processors` since it's request-scoped (not
    available outside dispatch).

    Idempotent — overwriting `env.filters[name]` with the same callable
    is cheap, so we re-sync on every render rather than caching. Safe to
    call when no app is bound (no-op).
    """
    from veloce.helpers import _current_app_var, current_app, g

    app = _current_app_var.get()
    if app is None:
        return
    # Standard template globals (TP8).
    env.globals["url_for"] = app.url_for
    env.globals["g"] = g
    env.globals["current_app"] = current_app
    for fname, fn in getattr(app, "_template_filters", ()):
        env.filters[fname] = fn
    for gname, fn in getattr(app, "_template_globals", ()):
        env.globals[gname] = fn
    for tname, fn in getattr(app, "_template_tests", ()):
        env.tests[tname] = fn


def _gather_context_processors(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run every `@app.context_processor` registered on the current app and
    merge their returned dicts. Returns an empty dict when no app is bound
    (e.g. rendering outside a request context).

    context-processor outputs are merged in registration order;
    the caller's explicit context (passed to `TemplateResponse`) wins over
    any conflicting key.
    """
    from veloce.helpers import _current_app_var

    app = _current_app_var.get()
    if app is None:
        return dict(extra or {})

    merged: dict[str, Any] = {}
    for processor in getattr(app, "_context_processors", ()):
        result = processor()
        # Async context processors are uncommon but legal; await only if
        # the result is a coroutine. The templating layer is sync, so we
        # can't actually await — async processors must be invoked from an
        # async context; here we just skip them with a clear failure mode.
        if inspect.iscoroutine(result):
            # The user declared an async context processor but called the
            # sync template path. Run it through asyncio.run is unsafe
            # inside an event loop, so skip with a warning attribute.
            result.close()  # avoid "coroutine was never awaited" warning
            continue
        if isinstance(result, dict):
            merged.update(result)
    if extra:
        # Caller's explicit context wins over context-processor defaults.
        merged.update(extra)
    return merged


class Jinja2Templates:
    """Jinja2 template engine integration.

    Usage:
        templates = Jinja2Templates(directory="templates")

        @app.get("/page")
        async def page(request: Request):
            return templates.TemplateResponse("page.html", {"request": request, "name": "World"})

    Any callables registered via `@app.context_processor` run before each
    render; their returned dicts are merged into the template context
    (caller's explicit context wins on collisions).
    """

    def __init__(
        self,
        directory: str = "templates",
        auto_reload: bool = True,
        autoescape: Any = None,
    ) -> None:
        try:
            from jinja2 import Environment, FileSystemLoader, select_autoescape
        except ImportError as err:
            raise ImportError(
                "jinja2 is required for templating. Install it: pip install jinja2"
            ) from err
        # the built-in default: autoescape HTML-shaped extensions. Matches
        # `select_autoescape(["html", "htm", "xhtml", "xml"])`. Pass an
        # explicit `autoescape=` to override (bool or callable).
        if autoescape is None:
            autoescape = select_autoescape(["html", "htm", "xhtml", "xml"])
        # `enable_async=False` so `Template.render(...)` is plain sync —
        # required because `TemplateResponse` is invoked inside an
        # already-running event loop, and `render` with `enable_async=True`
        # would `asyncio.run()` internally and crash.
        self.env = Environment(
            loader=FileSystemLoader(directory),
            auto_reload=auto_reload,
            enable_async=False,
            autoescape=autoescape,
        )
        # Lazily-built async-enabled twin used by `render_async`. Built
        # on first use so apps that never render async pay nothing.
        self._async_directory = directory
        self._async_auto_reload = auto_reload
        self._async_autoescape = autoescape
        self._async_env: Any = None

    def TemplateResponse(
        self,
        name: str,
        context: dict[str, Any],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> HTMLResponse:
        """Render a template and return as HTMLResponse."""
        _sync_app_jinja_helpers(self.env)
        template = self.env.get_template(name)
        merged = _gather_context_processors(context)
        html = template.render(merged)
        return HTMLResponse(content=html, status_code=status_code, headers=headers)

    def render(self, name: str, context: dict[str, Any] | None = None) -> str:
        """Render a named template to a string (no Response wrapping).

        Mirrors `TemplateResponse` but stops at the string stage so the
        `render_template(name, **ctx)` helper can plug in
        without building an HTMLResponse around the result.
        """
        _sync_app_jinja_helpers(self.env)
        template = self.env.get_template(name)
        merged = _gather_context_processors(context or {})
        return template.render(merged)

    def render_string(self, source: str, context: dict[str, Any]) -> str:
        """Render a template from string."""
        _sync_app_jinja_helpers(self.env)
        template = self.env.from_string(source)
        merged = _gather_context_processors(context)
        return template.render(merged)

    async def render_async(self, name: str, context: dict[str, Any] | None = None) -> str:
        """Asynchronously render a named template — Jinja `enable_async`.

        Uses a separate async-enabled `Environment` (built lazily) so
        `{% include %}`d templates with async I/O resolve without
        blocking the loop. Filters/globals registered on `app` are
        synced onto the async env too.
        """
        from jinja2 import Environment, FileSystemLoader

        if self._async_env is None:
            self._async_env = Environment(
                loader=FileSystemLoader(self._async_directory),
                auto_reload=self._async_auto_reload,
                enable_async=True,
                autoescape=self._async_autoescape,
            )
        _sync_app_jinja_helpers(self._async_env)
        template = self._async_env.get_template(name)
        merged = _gather_context_processors(context or {})
        return await template.render_async(merged)

    def get_template(self, name: str):
        """Get a raw Jinja2 template object."""
        return self.env.get_template(name)


# ── Module-level helpers ──────────────────────────────


def render_template(template_name: str, **context: Any) -> str:
    """Render a named template against the current app.

    Pulls the `Jinja2Templates` instance off `current_app._templates`
    (set when the user constructs a `Jinja2Templates(templates_dir)` and
    assigns it). Raises `RuntimeError` outside a request / app context.
    Returns the rendered string; callers wrap in a `Response` themselves
    if they need one.
    """
    from veloce.helpers import _current_app_var

    app = _current_app_var.get()
    if app is None:
        raise RuntimeError(
            "render_template requires an active application context "
            "(use it inside a request handler or `app.app_context()`)."
        )
    templates = getattr(app, "_templates", None)
    if templates is None:
        raise RuntimeError(
            "render_template requires a Jinja2Templates instance on "
            "`app._templates` — assign one after construction."
        )
    return templates.render(template_name, context)


def render_template_string(source: str, **context: Any) -> str:
    """Render an inline string template against the current app.

    Builds a transient Jinja2 environment when no `Jinja2Templates` is
    bound on the app, so the helper works for one-off templates that
    don't need a templates directory. Honours app-level filters /
    globals / tests and context processors when the env is reachable
    via `app._templates`.
    """
    from veloce.helpers import _current_app_var

    app = _current_app_var.get()
    templates = getattr(app, "_templates", None) if app is not None else None
    if templates is not None:
        return templates.render_string(source, context or {})

    # Fallback path: no `Jinja2Templates` bound. Use a minimal env so
    # the helper is still usable in scripts / tests.
    from jinja2 import Environment, select_autoescape

    env = Environment(autoescape=select_autoescape(["html", "htm", "xml", "xhtml"]))
    return env.from_string(source).render(context)
