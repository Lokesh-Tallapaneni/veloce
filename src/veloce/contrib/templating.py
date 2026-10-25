"""Jinja2 templating — lazy import, zero cost when unused."""

from __future__ import annotations

import contextvars
import inspect
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast
from weakref import WeakKeyDictionary

from veloce._internal import _current_app_var
from veloce.helpers import current_app, g, get_flashed_messages, request
from veloce.http.response import HTMLResponse, Response
from veloce.middleware.security import csp_nonce
from veloce.status import HTTP_200_OK

if TYPE_CHECKING:  # pragma: no cover
    from jinja2 import Environment


class _CSPNonceGlobal:
    """Template global resolving the current request's CSP nonce.

    Renders as the nonce itself, so the bare `<script nonce="{{ csp_nonce }}">`
    works; a call-only global would render its own repr into the attribute.
    Empty string when no nonce is armed, so a CSP-less page still renders.
    """

    __slots__ = ()

    def __call__(self) -> str:
        return csp_nonce() or ""

    def __str__(self) -> str:
        return csp_nonce() or ""

    def __bool__(self) -> bool:
        return bool(csp_nonce())

    def __repr__(self) -> str:
        return "<csp_nonce>"


# Stateless, so one instance serves every environment and every request.
_CSP_NONCE_GLOBAL = _CSPNonceGlobal()

# Resolved on lookup, not installed into `env.globals`, which Jinja copies per
# render - taxing templates that never name them.
_LAZY_TEMPLATE_NAMES: dict[str, Any] = {
    "request": request,
    "csp_nonce": _CSP_NONCE_GLOBAL,
}


# Built on first environment construction, not at import: `veloce` imports this
# module eagerly, and Jinja must not load in a process that never renders.
_context_class: Any = None


def _veloce_context_class() -> Any:
    """Return the render-context subclass resolving Veloce's template names."""
    global _context_class
    if _context_class is None:
        from jinja2.runtime import Context
        from jinja2.utils import missing

        class _VeloceContext(Context):
            """Render context that falls back to Veloce's request-scoped names.

            The explicit render context and `env.globals` are consulted first, so
            a caller passing `{"request": request}` still wins.
            """

            __slots__ = ()

            def resolve_or_missing(self, key: str) -> Any:
                value = super().resolve_or_missing(key)
                if value is missing:
                    return _LAZY_TEMPLATE_NAMES.get(key, missing)
                return value

        _context_class = _VeloceContext
    return _context_class


# Memoizes `_sync_app_jinja_helpers`; the token's counts invalidate it when a
# filter is registered after init.
_HELPER_SYNC_TOKEN_ATTR = "_veloce_helper_sync_token"

# `render_template_string`'s no-app fallback env, built once and reused.
_fallback_env: Environment | None = None
# One env per app, weak-keyed, so one app's filters never reach another's render.
_app_fallback_envs: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()


def _sync_app_jinja_helpers(env: Environment) -> None:
    """Copy filters/globals/tests registered on the active app into `env`.

    Also injects the standard template globals - `url_for`, `g`,
    `current_app`, and `get_flashed_messages` - so templates can
    `{{ url_for('endpoint') }}`, `{{ g.user }}`, and
    `{% for m in get_flashed_messages() %}` without manual context plumbing.
    `request` and `csp_nonce` are deliberately *not* installed here; they resolve
    through `context_class` instead, which costs only the templates naming them.

    Memoized per (env, app), on a token carrying the registration-list lengths,
    so a later `@app.template_filter` still invalidates it.
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
    # jinja2 types `Environment.globals` as its own default namespace.
    env_globals = cast("dict[str, Any]", env.globals)
    env_globals["url_for"] = app.url_for
    env_globals["g"] = g
    env_globals["current_app"] = current_app
    env_globals["get_flashed_messages"] = get_flashed_messages
    for fname, fn in filters:
        env.filters[fname] = fn
    for gname, fn in globs:
        env.globals[gname] = fn
    for tname, fn in tests:
        env.tests[tname] = fn
    setattr(env, _HELPER_SYNC_TOKEN_ATTR, token)


def _gather_context_processors(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the current app's `@app.context_processor`s and merge the results.

    Outputs merge in registration order, and the caller's own `extra` wins over
    any conflicting key. With no app bound, `extra` is returned unchanged.
    """
    app = _current_app_var.get()
    if app is None:
        return dict(extra or {})

    merged: dict[str, Any] = {}
    for processor in getattr(app, "_context_processors", ()):
        result = processor()
        # The sync path cannot await, so an async processor is skipped; close it
        # to avoid a "coroutine was never awaited" ResourceWarning.
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

    Awaits `async def` processors instead of discarding them; sync ones are
    called directly, and the caller's explicit context still wins.
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

    # A candidate list built from request-derived names would grow unbounded.
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
                "jinja2 is required for templating. Install it with: pip install jinja2"
            ) from err
        # The built-in default: autoescape HTML-shaped extensions.
        if autoescape is None:
            autoescape = select_autoescape(["html", "htm", "xhtml", "xml"])
        # `None` tracks the app's `debug` at render time, so production skips the
        # per-render `stat`; the Environment starts `True` for no-app rendering.
        self._auto_reload = auto_reload
        initial_reload = auto_reload if auto_reload is not None else True
        # `enable_async=False` keeps `render` sync: it is called inside an
        # already-running event loop, where the async form `asyncio.run`s and crashes.
        self.env = Environment(
            loader=FileSystemLoader(directory),
            auto_reload=initial_reload,
            enable_async=False,
            autoescape=autoescape,
        )
        self.env.context_class = _veloce_context_class()
        # The async twin for `render_async`, built on first use.
        self._async_directory = directory
        self._async_auto_reload = initial_reload
        self._async_autoescape = autoescape
        self._async_env: Environment | None = None
        # With `auto_reload` False, memoizes a fallback list's resolved winner.
        self._resolved_cache: dict[tuple[int, tuple[str, ...]], str] = {}

    def _prepare_env(self, env: Environment) -> None:
        """Bring `env` up to date for one render.

        Every render entry point opens with this, so a step added here reaches
        all of them.
        """
        self._apply_auto_reload(env)
        _sync_app_jinja_helpers(env)

    def _apply_auto_reload(self, env: Environment) -> None:
        """Track the bound app's `debug` flag when `auto_reload` was left unset.

        Production (`debug=False`) skips the per-render template `stat`.
        Explicit settings are left untouched.
        """
        if self._auto_reload is not None:
            return
        app = _current_app_var.get()
        if app is not None:
            env.auto_reload = bool(getattr(app, "debug", False))

    def _resolve_template(self, env: Environment, name: str | Sequence[str]) -> Any:
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
            # Evict oldest-first; `RESOLVED_CACHE_MAX <= 0` disables the cache.
            winner_name = tpl.name
            if self.RESOLVED_CACHE_MAX >= 1 and winner_name is not None:
                while len(self._resolved_cache) >= self.RESOLVED_CACHE_MAX:
                    del self._resolved_cache[next(iter(self._resolved_cache))]
                self._resolved_cache[key] = winner_name
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
        """Render a template and return a response.

        The content type may be overridden and a background task attached.
        """
        self._prepare_env(self.env)
        template = self._resolve_template(self.env, name)
        merged = _gather_context_processors(context)
        html = template.render(merged)
        if media_type is None:
            response: Response = HTMLResponse(
                content=html, status_code=status_code, headers=headers, background=background
            )
        else:
            response = Response(
                status_code=status_code,
                body=html.encode("utf-8"),
                content_type=media_type,
                headers=headers,
                background=background,
            )
        return response

    def render(self, name: str | Sequence[str], context: dict[str, Any] | None = None) -> str:
        """Render a named template to a string (no Response wrapping).

        Mirrors `TemplateResponse` but stops at the string stage so the
        `render_template(name, **ctx)` helper can plug in
        without building an HTMLResponse around the result.
        """
        self._prepare_env(self.env)
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
        self._prepare_env(self.env)
        template = self._resolve_template(self.env, name)
        merged = _gather_context_processors(context or {})
        return _context_preserving_iter(template.generate(merged))

    def render_string(self, source: str, context: dict[str, Any]) -> str:
        """Render a template from string."""
        self._prepare_env(self.env)
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
            self._async_env.context_class = _veloce_context_class()
        self._prepare_env(self._async_env)
        template = self._resolve_template(self._async_env, name)
        merged = await _gather_context_processors_async(context or {})
        return await template.render_async(merged)

    def get_template(self, name: str | Sequence[str]) -> Any:
        """Return a raw Jinja2 template object.

        A fallback list resolves to the first template that exists.
        """
        return self._resolve_template(self.env, name)


# ── Module-level helpers ──────────────────────────────────


def _templates_for(caller: str) -> Any:
    """Return the current app's `Jinja2Templates`, or raise saying which is missing.

    Both module-level helpers need the same two things and raised the same two
    messages, differing only in the verb naming the caller - so the messages had
    two places to be edited and one of them would eventually be missed.
    """
    app = _current_app_var.get()
    if app is None:
        raise RuntimeError(
            f"{caller} requires an active application context "
            "(use it inside a request handler or `app.app_context()`)."
        )
    templates = getattr(app, "_templates", None)
    if templates is None:
        raise RuntimeError(
            f"{caller} requires templates, which the app does not have. "
            'Construct it with a template folder: Veloce(template_folder="templates").'
        )
    return templates


def _new_fallback_env() -> Environment:
    """Build the minimal environment the string helper falls back to."""
    from jinja2 import Environment, select_autoescape

    env = Environment(autoescape=select_autoescape(["html", "htm", "xml", "xhtml"]))
    env.context_class = _veloce_context_class()
    return env


def render_template(template_name: str | Sequence[str], **context: Any) -> str:
    """Render a named template against the current app.

    Pulls the `Jinja2Templates` instance off `current_app._templates`
    (set when the user constructs a `Jinja2Templates(templates_dir)` and
    assigns it). Raises `RuntimeError` outside a request / app context.
    Returns the rendered string; callers wrap in a `Response` themselves
    if they need one.
    """
    return _templates_for("render_template").render(template_name, context)


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
    return _templates_for("stream_template").stream(template_name, context)


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

    # No `Jinja2Templates` bound: a minimal env, built once, carrying the app's
    # filters so they still resolve.
    if app is None:
        global _fallback_env
        if _fallback_env is None:
            _fallback_env = _new_fallback_env()
        return _fallback_env.from_string(source).render(context)

    env = _app_fallback_envs.get(app)
    if env is None:
        env = _app_fallback_envs[app] = _new_fallback_env()
    _sync_app_jinja_helpers(env)
    return env.from_string(source).render(context)
