"""Introspection and URL hooks — the read-only views and the `url_for` seam.

A mixin on `Veloce`, covering the `{endpoint: ...}` views a tool or template
reads and the URL side (`@app.url_value_preprocessor`, `@app.url_defaults`, and
the `url_for` / `url_path_for` pair). Every view is built on demand from the
route table and the hook lists, so `core` owns the state and this owns reading it.
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

        Named by the route's `name=` kwarg or the handler's `__name__`, prefixed
        with `<bpname>.` for a blueprint route. A fresh snapshot per call, so
        mutation does not poison framework state.
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

        Replaces the existing route's handler in place, which separates route
        declaration from view registration.
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            replaced = False
            for _method, _path, info in self._collect_all_routes():
                if info.name == name:
                    info.handler = func
                    info.description = info.description or (func.__doc__ or "")
                    replaced = True
                    # Rebuilt through the registration path, so every dispatch
                    # flag describes the replacement rather than the stub.
                    self._finalize_plans(info, is_ws=_method.upper() == ROUTE_METHOD_WEBSOCKET)
            if not replaced:
                raise ValueError(f"No route registered for endpoint {name!r}")
            # Dropped so the next read sees `func`, not the stub.
            self._cached_view_functions = None
            return func

        return decorator

    @property
    def error_handler_spec(self) -> dict[Any, dict[Any, Callable[..., Any]]]:
        """Inspection view of registered error handlers.

        A `{blueprint_name_or_None: {key: handler}}` mapping, keyed by integer
        status code or exception class. Blueprint handlers are scoped to their
        own routes at dispatch time, so they appear under their blueprint name
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

        `{blueprint_name_or_None: [hook, ...]}`. The dispatcher walks the `None`
        bucket plus the one matching the matched route's endpoint prefix.
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

        A fresh copy; re-registering a name overwrites the previous entry.
        """
        return dict(self._blueprints_map)

    def iter_blueprints(self) -> Any:
        """Iterate over every registered `Blueprint`.

        In registration order, yielding the objects rather than their names.
        """
        return iter(self._blueprints_map.values())

    @property
    def url_value_preprocessors(self) -> dict[Any, list[Callable[..., Any]]]:
        """View of registered URL-value preprocessors.

        `{blueprint_name_or_None: [fn, ...]}`, each blueprint under its dotted
        name. A nested blueprint's entry is the flattened chain that runs for
        its routes, outermost first.
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

        Each processor is called with no args; its dict joins the namespace the
        interactive shell starts with.
        """
        self._shell_context_processors.append(func)
        return func

    def make_shell_context(self) -> dict[str, Any]:
        """Build the dict the CLI's `shell` command drops into.

        Always includes `app` and `g`; each processor's dict overlays in
        registration order, so a later processor wins on conflicts.
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

        `endpoint` is the route name; `values` is the path_params dict, and
        mutating it in place is the supported way to rewrite them.
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
        appear in the rendered URL. On build failure each registered
        `app.url_build_error_handlers` callback is invoked with
        `(error, endpoint, values)` in order and the first non-None return is
        used; `BuildError` is raised if none recovers.
        """
        bp_defaults = None
        if self._bp_url_default_funcs:
            bp = _endpoint_blueprint(name)
            if bp is not None:
                bp_defaults = self._bp_url_default_funcs.get(bp)
        if self._url_default_funcs or bp_defaults:
            # Copied so a callback cannot mutate the caller's kwargs dict.
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
