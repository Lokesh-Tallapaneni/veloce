"""Blueprint primitive - a core building block.

A `Blueprint` is a deferred-registration collection of routes, hooks,
and error handlers that gets bound to an app via
`app.register_blueprint(bp, url_prefix=...)`. The blueprint itself owns
nothing at runtime - its routes are added to the app's radix tree at
registration time with the (optionally combined) prefix.

Scope of this implementation:

- routes via the standard `@bp.get/.post/.put/.patch/.delete/.head/
  .options/.route/.websocket` decorators.
- per-blueprint `before_request` and `after_request` hooks. They fire
  only for requests routed to a blueprint route.
- per-blueprint `errorhandler` (alias `exception_handler`). Catches
  exceptions raised by blueprint handlers; falls through to the app's
  global handlers when no blueprint-level match.
- `url_prefix` set at construction or override at registration.
- Nested blueprints are *not* supported in this slice - that's R4,
  separate work.

`Blueprint` extends `Router`, so blueprint-level routes inherit the
radix-tree builder and `default_response_class` plumbing for free.
The registration step splices the blueprint's collected routes into
the app's own tree under the combined prefix; the blueprint instance
keeps the route registrations cached so `register_blueprint` can be
called multiple times on different apps.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from veloce.routing.router import RouteInfo, Router


def _endpoint_blueprint(endpoint: str | None) -> str | None:
    """Return the blueprint name encoded in an endpoint, or `None`.

    Blueprint routes have endpoints of the form `"{bp}.{routename}"`;
    app-level routes have a bare `"routename"` (no dot). This is the
    same convention `register_blueprint` and `url_for` already use.
    """
    if not endpoint:
        return None
    dot = endpoint.find(".")
    return endpoint[:dot] if dot >= 0 else None


class Blueprint(Router):
    """Deferred-registration route collection."""

    def __init__(
        self,
        name: str,
        url_prefix: str = "",
        default_response_class: Any = None,
        dependencies: list | None = None,
        responses: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            prefix=url_prefix,
            default_response_class=default_response_class,
            dependencies=dependencies,
            responses=responses,
        )
        self.name = name
        self.url_prefix = url_prefix
        # Pending hook + handler registrations - applied to the app at
        # `register_blueprint` time.
        self._before_request_hooks: list[Callable] = []
        self._after_request_hooks: list[Callable] = []
        self._teardown_request_hooks: list[Callable] = []
        self._exception_handlers: dict[type, Callable] = {}
        self._status_handlers: dict[int, Callable] = {}
        # URL processors registered on this blueprint. They're
        # gated to blueprint endpoints at register time.
        self._url_value_preprocessors: list[Callable] = []
        self._url_default_funcs: list[Callable] = []

    # -- Hook decorators -----------------------------------

    def before_request(self, func: Callable) -> Callable:
        """Register a function to run before each blueprint request.

        Fires only for requests that match a route declared on this
        blueprint. Use `app.before_request` for app-wide hooks.
        """
        self._before_request_hooks.append(func)
        return func

    def after_request(self, func: Callable) -> Callable:
        """Register a function to run after each blueprint request."""
        self._after_request_hooks.append(func)
        return func

    def teardown_request(self, func: Callable) -> Callable:
        """Run after blueprint-routed request teardown, with optional exc."""
        self._teardown_request_hooks.append(func)
        return func

    def errorhandler(self, exc_class_or_status: type | int) -> Callable:
        """Blueprint-scoped error handler.

        Matches `app.errorhandler` semantics: integer keys go to the
        status-code table, classes go to the MRO-matched exception
        table. The handler runs for exceptions raised by blueprint
        handlers; app-level handlers act as fallback (registration
        order: blueprint wins on direct match).
        """

        def decorator(func: Callable) -> Callable:
            if isinstance(exc_class_or_status, int):
                self._status_handlers[exc_class_or_status] = func
            else:
                self._exception_handlers[exc_class_or_status] = func
            return func

        return decorator

    exception_handler = errorhandler

    def url_value_preprocessor(self, func: Callable) -> Callable:
        """Register a `fn(endpoint, values)` URL preprocessor on this blueprint.

        Mirrors `@app.url_value_preprocessor` (R20) - runs after route
        match for blueprint-routed requests, mutating `values` in
        place. Use to pop a path-param into `g` (e.g. a lang segment)
        before the handler sees it.
        """
        self._url_value_preprocessors.append(func)
        return func

    def url_defaults(self, func: Callable) -> Callable:
        """Register a `fn(endpoint, values)` URL-defaults injector for `url_for`.

        Mirrors `@app.url_defaults` (R21) - runs inside `url_for` /
        `url_path_for` for endpoints belonging to this blueprint. Use
        `values.setdefault(...)` for caller-wins semantics.
        """
        self._url_default_funcs.append(func)
        return func

    # -- Nested blueprints (R4) ----------------------------

    def register_blueprint(
        self,
        child: Blueprint,
        url_prefix: str | None = None,
    ) -> None:
        """Mount another blueprint as a sub-blueprint of this one.

        Routes from `child` register under
        `self.url_prefix + (url_prefix or child.url_prefix) + path`;
        endpoint names stored on this blueprint become
        `<child.name>.<handler>` and pick up the `<self.name>.` prefix
        once *this* blueprint is itself registered with an app, yielding
        a final `<self.name>.<child.name>.<handler>` lookup name so the
        dispatcher's prefix-gate finds them under either name.

        Hooks and error handlers from `child` are merged into this
        blueprint's lists (not the app's - the app gets them when
        *this* blueprint is registered).
        """
        if child is self:
            raise ValueError(f"Cannot register blueprint {self.name!r} as a child of itself.")
        nested_prefix = url_prefix if url_prefix is not None else child.url_prefix
        for path, methods, info in child._walk_routes():
            full_path = (nested_prefix or "") + path
            endpoint = f"{child.name}.{info.name}"
            self.add_route(
                path=full_path,
                handler=info.handler,
                methods=methods,
                dependencies=info.dependencies,
                response_model=info.response_model,
                tags=info.tags,
                summary=info.summary,
                name=endpoint,
                description=info.description,
                deprecated=info.deprecated,
                response_description=info.response_description,
                status_code=info.status_code,
                response_class=info.response_class,
                response_model_include=info.response_model_include,
                response_model_exclude=info.response_model_exclude,
                response_model_exclude_unset=info.response_model_exclude_unset,
                response_model_exclude_defaults=info.response_model_exclude_defaults,
                response_model_by_alias=info.response_model_by_alias,
                response_model_exclude_none=info.response_model_exclude_none,
                include_in_schema=info.include_in_schema,
                responses=info.responses,
                operation_id=info.operation_id,
                openapi_extra=info.openapi_extra,
                defaults=info.defaults,
                callbacks=info.callbacks,
                subdomain=info.subdomain,
                host=info.host,
                expose_as_mcp_tool=info.expose_as_mcp_tool,
                mcp_description=info.mcp_description,
            )

        # Inherit child hooks + error handlers. Child's hooks will be
        # endpoint-gated when *this* blueprint registers onto the app
        # (parent gate covers `<parent_name>.` which matches
        # `<parent_name>.<child_name>....`).
        self._before_request_hooks.extend(child._before_request_hooks)
        self._after_request_hooks.extend(child._after_request_hooks)
        self._teardown_request_hooks.extend(child._teardown_request_hooks)
        for exc_cls, handler in child._exception_handlers.items():
            self._exception_handlers.setdefault(exc_cls, handler)
        for code, handler in child._status_handlers.items():
            self._status_handlers.setdefault(code, handler)
        self._url_value_preprocessors.extend(child._url_value_preprocessors)
        self._url_default_funcs.extend(child._url_default_funcs)

    # -- Route collection inspection - used by register_blueprint ---

    def _walk_routes(self) -> list[tuple[str, list[str], RouteInfo]]:
        """Return `(path, methods, RouteInfo)` triples for every route.

        The blueprint's own `url_prefix` is **stripped** from each path
        before returning, so `register_blueprint` can re-apply a
        (possibly different) prefix without double-nesting. Caller is
        expected to prepend the chosen prefix.
        """
        results: list[tuple[str, list[str], RouteInfo]] = []
        own_prefix = self.url_prefix.rstrip("/")
        # include_hidden=True: a blueprint's WebSocket routes and its
        # include_in_schema=False routes must still enter the app's tree.
        for method, _path, info in self._collect_all_routes(include_hidden=True):
            # `_walk_tree` reconstructs the path from the radix structure,
            # which loses the trailing-slash distinction the router
            # collapses at storage time (both `@bp.get("/")` and
            # `@bp.get("")` map to the same radix node). Read from
            # `RouteInfo.path_template` instead - it carries the original
            # `prefix + user_path` string so we can tell the two apart
            # and re-prefix correctly.
            full_path = info.path_template
            if own_prefix and full_path.startswith(own_prefix):
                # Strip the prefix verbatim - preserving an empty
                # remainder (`@bp.get("")` against the bare prefix) and
                # an explicit `/` remainder (`@bp.get("/")`) as their
                # own distinct shapes for the re-prefix step.
                stripped = full_path[len(own_prefix) :]
            else:
                stripped = full_path
            results.append((stripped, [method], info))
        return results
