"""Introspection and URL hooks — the read-only views and the `url_for` seam.

A mixin on `Veloce`. Two related surfaces, both about *reading* or *shaping* the
route table rather than building it: the `{endpoint: ...}` views a tool or a
template reaches for (`view_functions`, `error_handler_spec`, the four hook
maps, `blueprints`), and the URL side - `@app.url_value_preprocessor`,
`@app.url_defaults`, and the `url_for` / `url_path_for` pair that runs the
defaults callbacks before delegating to `Router.url_for`.

Every view is built on demand from the route table and the hook lists, so none
of it holds state of its own; `core` owns the state, this owns the reading of
it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veloce._protocol_constants import ROUTE_METHOD_WEBSOCKET, URL_SCHEME_HTTP
from veloce.app._host import AppHost
from veloce.blueprints import _endpoint_blueprint
from veloce.exceptions import BuildError
from veloce.helpers import g

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable


class IntrospectionMixin(AppHost):
    """Read-only views over the route table, and the URL-building hooks."""

    # ── Endpoint and hook introspection ────────────────────

    @property
    def view_functions(self) -> dict[str, Callable[..., Any]]:
        """A `{endpoint_name: handler}` view of registered routes.

        Endpoint names follow a simple rule - the route's `name=`
        kwarg, or the handler's `__name__` when no name is set; blueprint
        routes are prefixed with `<bpname>.`. Returned dict is a fresh
        snapshot - mutation doesn't poison framework state.
        """
        cached = self._cached_view_functions
        if cached is None:
            cached = {}
            for _method, _path, info in self._collect_all_routes():
                cached[info.name] = info.handler
            self._cached_view_functions = cached
        return dict(cached)

    def endpoint(self, name: str) -> Callable[..., Any]:
        """Attach a function as the view for an already-registered `name`.

        Useful when separating route declaration (via
        `app.add_url_rule(rule, endpoint="x")`) from view registration.
        Replaces the existing route's handler in place.
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            replaced = False
            for _method, _path, info in self._collect_all_routes():
                if info.name == name:
                    info.handler = func
                    info.description = info.description or (func.__doc__ or "")
                    replaced = True
                    # Rebuild the plans through the same path registration
                    # uses, so every dispatch flag - including
                    # `is_fast_eligible`, which depends on the handler being a
                    # coroutine function - reflects the replacement handler
                    # rather than the stub it displaced.
                    self._finalize_plans(info, is_ws=_method.upper() == ROUTE_METHOD_WEBSOCKET)
            if not replaced:
                raise ValueError(f"No route registered for endpoint {name!r}")
            # The name -> handler map served by `view_functions` may have been
            # built against the stub; drop it so the next read sees `func`.
            self._cached_view_functions = None
            return func

        return decorator

    @property
    def error_handler_spec(self) -> dict[Any, dict[Any, Callable[..., Any]]]:
        """Inspection view of registered error handlers.

        Returns a `{blueprint_name_or_None: {key: handler}}` mapping.
        App-level handlers live under the `None` key; each blueprint's
        handlers live under the blueprint's name, keyed by integer status
        code or exception class. Blueprint handlers are scoped to their own
        routes at dispatch time, so they appear under their blueprint name
        here, not folded into `None`.
        """
        merged: dict[Any, Callable[..., Any]] = {}
        merged.update(self._status_handlers)
        merged.update(self._exception_handlers)
        result: dict[Any, dict[Any, Callable[..., Any]]] = {None: merged}
        for bp_name in set(self._bp_status_handlers) | set(self._bp_exception_handlers):
            sub: dict[Any, Callable[..., Any]] = {}
            sub.update(self._bp_status_handlers.get(bp_name, {}))
            sub.update(self._bp_exception_handlers.get(bp_name, {}))
            result[bp_name] = sub
        return result

    @property
    def before_request_funcs(self) -> dict[Any, list[Callable[..., Any]]]:
        """View of registered `before_request` hooks.

        Returns `{blueprint_name_or_None: [hook, ...]}`. App-level hooks
        live under the `None` key; blueprint hooks under the blueprint's
        name. The dispatcher walks the `None` bucket plus the bucket
        whose name matches the matched route's endpoint prefix.
        """
        result: dict[Any, list[Callable[..., Any]]] = {None: list(self._before_request_hooks)}
        for bp, hooks in self._bp_before_hooks.items():
            result[bp] = list(hooks)
        return result

    @property
    def after_request_funcs(self) -> dict[Any, list[Callable[..., Any]]]:
        """Return the per-blueprint after-request hook registry."""
        result: dict[Any, list[Callable[..., Any]]] = {None: list(self._after_request_hooks)}
        for bp, hooks in self._bp_after_hooks.items():
            result[bp] = list(hooks)
        return result

    @property
    def teardown_request_funcs(self) -> dict[Any, list[Callable[..., Any]]]:
        """Return the per-blueprint teardown-request hook registry."""
        result: dict[Any, list[Callable[..., Any]]] = {None: list(self._teardown_request_hooks)}
        for bp, hooks in self._bp_teardown_hooks.items():
            result[bp] = list(hooks)
        return result

    @property
    def blueprints(self) -> dict[str, Any]:
        """Snapshot mapping of `bp.name -> Blueprint`.

        Returns a fresh copy, so caller mutations don't affect the
        framework. Re-registering the same name overwrites the previous
        entry.
        """
        return dict(self._blueprints_map)

    def iter_blueprints(self) -> Any:
        """Iterate over every registered `Blueprint`.

        Returns the blueprints in registration order (Python 3.7+ dict
        insertion order). Yields the Blueprint objects, not their names.
        """
        return iter(self._blueprints_map.values())

    @property
    def url_value_preprocessors(self) -> dict[Any, list[Callable[..., Any]]]:
        """View of registered URL-value preprocessors.

        Returns `{blueprint_name_or_None: [fn, ...]}` - app-level processors
        under `None`, then each blueprint's under its dotted name. A nested
        blueprint's entry is the flattened chain that applies to its routes,
        outermost first, which is what runs.
        """
        view: dict[Any, list[Callable[..., Any]]] = {None: list(self._url_value_preprocessors)}
        view.update({name: list(fns) for name, fns in self._bp_url_value_preprocessors.items()})
        return view

    @property
    def url_default_functions(self) -> dict[Any, list[Callable[..., Any]]]:
        """View of registered URL-default callbacks, keyed as `url_value_preprocessors`."""
        view: dict[Any, list[Callable[..., Any]]] = {None: list(self._url_default_funcs)}
        view.update({name: list(fns) for name, fns in self._bp_url_default_funcs.items()})
        return view

    def shell_context_processor(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Register a function returning a dict to merge into `veloce shell`.

        each processor is called with no args; its dict
        becomes part of the namespace the interactive shell starts with.
        Useful for surfacing models / db sessions / common helpers so
        `User.query.first()` works without a manual `from myapp.models
        import User` every time.
        """
        self._shell_context_processors.append(func)
        return func

    def make_shell_context(self) -> dict[str, Any]:
        """Build the dict the CLI's `shell` command drops into.

        Always includes `app` (this Veloce instance) and `g`. Each
        registered shell-context-processor's return dict overlays on
        top, in registration order - later processors win on conflicts.
        """
        ctx: dict[str, Any] = {"app": self, "g": g}
        for fn in self._shell_context_processors:
            extra = fn()
            if extra:
                ctx.update(extra)
        return ctx

    # ── URL processors (URL hooks) ────────────────────────

    def url_value_preprocessor(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Register a callback that may mutate the matched path params.

        Called as `fn(endpoint, values)` before the handler runs.

        Usage::

            @app.url_value_preprocessor
            def pull_lang(endpoint, values):
                from veloce import g
                g.lang = values.pop("lang", "en")

        `endpoint` is the route name; `values` is the path_params dict
        (mutating it in place is the supported way to remove / rewrite
        values before the handler sees them).
        """
        self._assert_mutable()
        self._url_value_preprocessors.append(func)
        self._gen += 1
        return func

    def _absolute_url_defaults(self) -> tuple[str | None, str]:
        """Answer `Router`'s absolute-URL hook from this app's configuration."""
        return (
            self.config.get("SERVER_NAME"),
            self.config.get("PREFERRED_URL_SCHEME", URL_SCHEME_HTTP),
        )

    def url_for(self, name: str, /, **path_params: Any) -> str:
        """Build the URL for `name`, applying the `@app.url_defaults` callbacks.

        They run before delegating to `Router.url_for`, so injected defaults
        appear in the rendered URL.

        On build failure (unknown endpoint or missing path parameter),
        each registered `app.url_build_error_handlers` callback is
        invoked with `(error, endpoint, values)` in order; the first
        non-None return is used. If none recovers, a `BuildError` is
        raised.
        """
        bp_defaults = None
        if self._bp_url_default_funcs:
            bp = _endpoint_blueprint(name)
            if bp is not None:
                bp_defaults = self._bp_url_default_funcs.get(bp)
        if self._url_default_funcs or bp_defaults:
            # Copy so the callbacks can mutate without changing the caller's
            # kwargs dict.
            values = dict(path_params)
            for fn in self._url_default_funcs:
                fn(name, values)
            for fn in bp_defaults or ():
                fn(name, values)
        else:
            values = path_params

        try:
            built: str = super().url_for(name, **values)  # type: ignore[misc]
        except (ValueError, KeyError) as exc:
            return self._handle_build_error(name, values, exc)
        return built

    def _handle_build_error(self, name: str, values: dict[str, Any], exc: Exception) -> str:
        """Offer a failed build to `url_build_error_handlers` before raising."""
        err = BuildError(name, values)
        err.__cause__ = exc
        for handler in self.url_build_error_handlers:
            result = handler(err, name, values)
            if result is not None:
                built: str = result
                return built
        raise err from exc

    # Keep `url_path_for` aligned with the override above.
    def url_path_for(self, name: str, /, **path_params: Any) -> str:
        """Resolve a URL path by endpoint name and parameters."""
        return self.url_for(name, **path_params)

    def url_defaults(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Register a callback injecting default kwargs into every URL build.

        Called as `fn(endpoint, values)` from `url_for` and `url_path_for`.

        Usage::

            @app.url_defaults
            def add_lang(endpoint, values):
                from veloce import g
                values.setdefault("lang", g.get("lang", "en"))

        Runs in registration order; mutate `values` in place.
        """
        self._assert_mutable()
        self._url_default_funcs.append(func)
        return func
