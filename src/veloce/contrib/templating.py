"""Jinja2 templating — lazy import, zero cost when unused."""

from __future__ import annotations

import contextvars
import inspect
from collections.abc import Sequence
from typing import Any

from veloce.background import BackgroundTask
from veloce.helpers import _current_app_var, current_app, g
from veloce.http.response import HTMLResponse, Response
from veloce.status import HTTP_200_OK

# Sentinel attribute name written onto each Jinja Environment to memoize
# the result of `_sync_app_jinja_helpers`. Holds a (id(app), filter/global/
# test counts) tuple - bumps invalidate the cache when the user registers
# a new filter / global / test after init.
_HELPER_SYNC_TOKEN_ATTR = "_veloce_helper_sync_token"

# Memoized transient Jinja Environment for `render_template_string`'s
# no-app fallback path. Built once on first fallback render and reused
# thereafter so repeated string renders skip per-call env construction.
_fallback_env: Any = None


def _sync_app_jinja_helpers(env: Any) -> None:
    """Copy filters/globals/tests registered on the active app into `env`.

    Also injects the standard template globals - `url_for`, `g`, and
    `current_app` - so templates can `{{ url_for('endpoint') }}` and
    `{{ g.user }}` without manual context plumbing. `request` is not injected
    here; being request-scoped, it is supplied by the caller's render context
    (e.g. `TemplateResponse("page.html", {"request": request, ...})`).

    Memoized per (env, app) - re-syncing is idempotent but it still
    runs three loops and several attribute lookups per render, which
    is wasted work on every request once filters are stable. The
    token includes the registration-list lengths, so adding a new
    filter / global / test through `@app.template_filter` etc.
    invalidates the cache and the next render re-syncs.
    """
    app = _current_app_var.get()
    if app is None:
        return
    filters = getattr(app, "_template_filters", ())
    globs = getattr(app, "_template_globals", ())
    tests = getattr(app, "_template_tests", ())
    token = (id(app), len(filters), len(globs), len(tests))
    if getattr(env, _HELPER_SYNC_TOKEN_ATTR, None) == token:
        return
    env.globals["url_for"] = app.url_for
    env.globals["g"] = g
    env.globals["current_app"] = current_app
    for fname, fn in filters:
        env.filters[fname] = fn
    for gname, fn in globs:
        env.globals[gname] = fn
    for tname, fn in tests:
        env.tests[tname] = fn
    setattr(env, _HELPER_SYNC_TOKEN_ATTR, token)


def _gather_context_processors(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run every `@app.context_processor` registered on the current app and
    merge their returned dicts. Returns an empty dict when no app is bound
    (e.g. rendering outside a request context).

    context-processor outputs are merged in registration order;
    the caller's explicit context (passed to `TemplateResponse`) wins over
    any conflicting key.
    """
    app = _current_app_var.get()
    if app is None:
        return dict(extra or {})

    merged: dict[str, Any] = {}
    for processor in getattr(app, "_context_processors", ()):
        result = processor()
        # The sync template path cannot await, so an async context processor's
        # values are skipped here; close the coroutine to avoid a "coroutine was
        # never awaited" ResourceWarning. Use the async render path
        # (`render_async` / `_gather_context_processors_async`) to run async
        # context processors.
        if inspect.iscoroutine(result):
            result.close()
            continue
        if isinstance(result, dict):
            merged.update(result)
    if extra:
        # Caller's explicit context wins over context-processor defaults.
        merged.update(extra)
    return merged


async def _gather_context_processors_async(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Async variant of `_gather_context_processors` for the async render path.

    Awaits `async def` context processors instead of discarding them, so they
    contribute values during `render_async`. Sync processors are called
    directly; the caller's explicit context still wins over their defaults.
    """
    app = _current_app_var.get()
    if app is None:
        return dict(extra or {})

    merged: dict[str, Any] = {}
    for processor in getattr(app, "_context_processors", ()):
        result = processor()
        if inspect.iscoroutine(result):
            result = await result
        if isinstance(result, dict):
            merged.update(result)
    if extra:
        merged.update(extra)
    return merged


def _context_preserving_iter(iterator: Any) -> Any:
    """Wrap a lazy sync iterator so each step runs in the caller's context.

    A streamed template body is consumed by the response-emit layer after the
    handler returns - on the built-in server, from a separate task whose
    context lacks the request-scoped `current_app` / `g` / `request`. Jinja
    resolves globals like `url_for` during iteration, so without this the
    stream would raise "working outside of application context". A snapshot of
    the context is captured now and each `next()` is driven through it via
    `ctx.run`, keeping the iterator synchronous (it stays a `str` generator,
    consumable by `list(...)` / `"".join(...)` and by `StreamingResponse`).
    """
    ctx = contextvars.copy_context()
    _sentinel = object()

    def _step() -> Any:
        return next(iterator, _sentinel)

    while True:
        chunk = ctx.run(_step)
        if chunk is _sentinel:
            return
        yield chunk


def _coerce_background(background: Any) -> Any:
    """Normalize a `TemplateResponse(background=...)` value to a task.

    Accepts `None`, an existing `BackgroundTask` / `BackgroundTasks`
    (duck-typed via `run` / `run_all`), or a bare callable wrapped in a
    no-arg `BackgroundTask`. Anything else is rejected.
    """
    if background is None:
        return None
    if hasattr(background, "run") or hasattr(background, "run_all"):
        return background
    if callable(background):
        return BackgroundTask(background)
    raise TypeError("background must be a callable, BackgroundTask, BackgroundTasks, or None")


class Jinja2Templates:
    """Jinja2 template engine integration.

    Usage::

        templates = Jinja2Templates(directory="templates")

        @app.get("/page")
        async def page(request: Request):
            return templates.TemplateResponse("page.html", {"request": request, "name": "World"})

    Any callables registered via `@app.context_processor` run before each
    render; their returned dicts are merged into the template context
    (caller's explicit context wins on collisions).
    """

    # Per-instance bound on the resolved-fallback-list cache. Each entry is a
    # distinct candidate sequence resolved in production (`auto_reload` False).
    # Candidate lists come from code, so the key space is normally tiny, but a
    # caller that builds fallback lists from request-derived names could grow
    # the dict without bound - cap it the same way the static ETag cache is.
    # Eviction only costs a re-run of `select_template` on a cold key.
    RESOLVED_CACHE_MAX = 1024

    def __init__(
        self,
        directory: str = "templates",
        auto_reload: bool | None = None,
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
        # `auto_reload=None` (the default) tracks the bound app's `debug`
        # flag at render time - on in development, off in production,
        # where the per-render template `stat` is pure overhead. Pass an
        # explicit `True`/`False` to pin it. The Environment starts
        # `auto_reload=True` so standalone (no-app) rendering still
        # hot-reloads.
        self._auto_reload = auto_reload
        initial_reload = auto_reload if auto_reload is not None else True
        # `enable_async=False` so `Template.render(...)` is plain sync -
        # required because `TemplateResponse` is invoked inside an
        # already-running event loop, and `render` with `enable_async=True`
        # would `asyncio.run()` internally and crash.
        self.env = Environment(
            loader=FileSystemLoader(directory),
            auto_reload=initial_reload,
            enable_async=False,
            autoescape=autoescape,
        )
        # Lazily-built async-enabled twin used by `render_async`. Built
        # on first use so apps that never render async pay nothing.
        self._async_directory = directory
        self._async_auto_reload = initial_reload
        self._async_autoescape = autoescape
        self._async_env: Any = None
        # Memoizes the winning name of a resolved fallback list per
        # `(id(env), candidates)` when `env.auto_reload` is False, so a
        # production render of a candidate sequence skips Jinja's
        # `select_template` stat walk after the first resolution.
        self._resolved_cache: dict[tuple[int, tuple[str, ...]], str] = {}

    def _apply_auto_reload(self, env: Any) -> None:
        """When `auto_reload` was left unset, track the bound app's
        `debug` flag - production (`debug=False`) skips the per-render
        template `stat`. Explicit settings are left untouched."""
        if self._auto_reload is not None:
            return
        app = _current_app_var.get()
        if app is not None:
            env.auto_reload = bool(getattr(app, "debug", False))

    def _resolve_template(self, env: Any, name: str | Sequence[str]) -> Any:
        """Load `name`, or the first existing template when `name` is a list.

        A plain `str` takes Jinja's `get_template` fast path unchanged. A
        sequence of candidates resolves to the first one that exists via
        `select_template`; in production (`auto_reload` False) the winning
        name is memoized so repeat renders skip the filesystem walk, while
        dev (`auto_reload` True) re-resolves every call so newly-added
        override templates are picked up.
        """
        if isinstance(name, str):
            return env.get_template(name)
        candidates = tuple(name)
        if env.auto_reload:
            return env.select_template(list(candidates))
        key = (id(env), candidates)
        winner = self._resolved_cache.get(key)
        if winner is None:
            tpl = env.select_template(list(candidates))
            # Bound the cache: evict the oldest entries (a plain dict preserves
            # insertion order, so the first key is the oldest) down to below the
            # cap before inserting. `RESOLVED_CACHE_MAX <= 0` disables the cache
            # entirely - a natural way for a user to turn it off - and the
            # `>= 1` guard means `next(iter(...))` is never called on an empty
            # dict; the `while` also absorbs the cap being lowered at runtime.
            if self.RESOLVED_CACHE_MAX >= 1:
                while len(self._resolved_cache) >= self.RESOLVED_CACHE_MAX:
                    del self._resolved_cache[next(iter(self._resolved_cache))]
                self._resolved_cache[key] = tpl.name
            return tpl
        return env.get_template(winner)

    def TemplateResponse(
        self,
        name: str | Sequence[str],
        context: dict[str, Any],
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
        *,
        media_type: str | None = None,
        background: Any = None,
    ) -> Response:
        """Render a template and return a response, optionally overriding the
        content type and attaching a background task."""
        self._apply_auto_reload(self.env)
        _sync_app_jinja_helpers(self.env)
        template = self._resolve_template(self.env, name)
        merged = _gather_context_processors(context)
        html = template.render(merged)
        if media_type is None:
            response: Response = HTMLResponse(
                content=html, status_code=status_code, headers=headers
            )
        else:
            response = Response(
                status_code=status_code,
                body=html.encode("utf-8"),
                content_type=media_type,
                headers=headers,
            )
        response.background = _coerce_background(background)
        return response

    def render(self, name: str | Sequence[str], context: dict[str, Any] | None = None) -> str:
        """Render a named template to a string (no Response wrapping).

        Mirrors `TemplateResponse` but stops at the string stage so the
        `render_template(name, **ctx)` helper can plug in
        without building an HTMLResponse around the result.
        """
        self._apply_auto_reload(self.env)
        _sync_app_jinja_helpers(self.env)
        template = self._resolve_template(self.env, name)
        merged = _gather_context_processors(context or {})
        return template.render(merged)

    def stream(self, name: str | Sequence[str], context: dict[str, Any] | None = None) -> Any:
        """Render a named template incrementally, yielding `str` chunks.

        Mirrors `render` but returns a synchronous iterator of `str` chunks
        instead of a fully-rendered string, so large templates can be
        streamed to the client without buffering the whole body. Wrap it in
        a `StreamingResponse` to return it from a handler.

        Jinja's generator is lazy - chunks render as the response body is
        consumed, which on the built-in server happens on a separate task
        after the handler returns. Each chunk is therefore produced inside a
        snapshot of the current context (`current_app`, `g`, `request`), so a
        template that reads them or calls `url_for` resolves correctly during
        emission instead of raising "working outside of application context".
        The returned iterator is still synchronous, preserving the contract.
        """
        self._apply_auto_reload(self.env)
        _sync_app_jinja_helpers(self.env)
        template = self._resolve_template(self.env, name)
        merged = _gather_context_processors(context or {})
        return _context_preserving_iter(template.generate(merged))

    def render_string(self, source: str, context: dict[str, Any]) -> str:
        """Render a template from string."""
        self._apply_auto_reload(self.env)
        _sync_app_jinja_helpers(self.env)
        template = self.env.from_string(source)
        merged = _gather_context_processors(context)
        return template.render(merged)

    async def render_async(
        self, name: str | Sequence[str], context: dict[str, Any] | None = None
    ) -> str:
        """Asynchronously render a named template - Jinja `enable_async`.

        Uses a separate async-enabled `Environment` (built lazily) so
        `{% include %}`d templates with async I/O resolve without
        blocking the loop. Filters/globals registered on `app` are
        synced onto the async env too.
        """
        if self._async_env is None:
            from jinja2 import Environment, FileSystemLoader

            self._async_env = Environment(
                loader=FileSystemLoader(self._async_directory),
                auto_reload=self._async_auto_reload,
                enable_async=True,
                autoescape=self._async_autoescape,
            )
        self._apply_auto_reload(self._async_env)
        _sync_app_jinja_helpers(self._async_env)
        template = self._resolve_template(self._async_env, name)
        merged = await _gather_context_processors_async(context or {})
        return await template.render_async(merged)

    def get_template(self, name: str | Sequence[str]) -> Any:
        """Get a raw Jinja2 template object, resolving a fallback list to the
        first existing template."""
        return self._resolve_template(self.env, name)


# -- Module-level helpers ---------------------------------------------


def render_template(template_name: str | Sequence[str], **context: Any) -> str:
    """Render a named template against the current app.

    Pulls the `Jinja2Templates` instance off `current_app._templates`
    (set when the user constructs a `Jinja2Templates(templates_dir)` and
    assigns it). Raises `RuntimeError` outside a request / app context.
    Returns the rendered string; callers wrap in a `Response` themselves
    if they need one.
    """
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
            "`app._templates` - assign one after construction."
        )
    return templates.render(template_name, context)


def stream_template(template_name: str | Sequence[str], **context: Any) -> Any:
    """Stream a named template against the current app, chunk by chunk.

    Mirrors `render_template` but returns an iterator of `str` chunks
    (Jinja's `template.generate(...)`) instead of a single string, so a
    large response body is produced lazily. Pulls the `Jinja2Templates`
    instance off `current_app._templates`; raises `RuntimeError` outside
    a request / app context. Wrap the result in a `StreamingResponse` to
    return it from a handler::

        from veloce import StreamingResponse, stream_template

        @app.get("/big")
        async def big(request):
            return StreamingResponse(stream_template("big.html", rows=rows))
    """
    app = _current_app_var.get()
    if app is None:
        raise RuntimeError(
            "stream_template requires an active application context "
            "(use it inside a request handler or `app.app_context()`)."
        )
    templates = getattr(app, "_templates", None)
    if templates is None:
        raise RuntimeError(
            "stream_template requires a Jinja2Templates instance on "
            "`app._templates` - assign one after construction."
        )
    return templates.stream(template_name, context)


def render_template_string(source: str, **context: Any) -> str:
    """Render an inline string template against the current app.

    Builds a transient Jinja2 environment when no `Jinja2Templates` is
    bound on the app, so the helper works for one-off templates that
    don't need a templates directory. Honours app-level filters /
    globals / tests and context processors when the env is reachable
    via `app._templates`.
    """
    app = _current_app_var.get()
    templates = getattr(app, "_templates", None) if app is not None else None
    if templates is not None:
        return templates.render_string(source, context or {})

    # Fallback path: no `Jinja2Templates` bound. Build a minimal env once
    # and reuse it so the helper stays usable in scripts / tests without
    # reconstructing the environment on every call.
    global _fallback_env
    if _fallback_env is None:
        from jinja2 import Environment, select_autoescape

        _fallback_env = Environment(autoescape=select_autoescape(["html", "htm", "xml", "xhtml"]))
    return _fallback_env.from_string(source).render(context)
