"""Blueprint primitive — a core building block.

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
- nested blueprints via `bp.register_blueprint(child)`, which scope the
  child's hooks and error handlers and give it a dotted endpoint name.

`Blueprint` extends `Router`, so blueprint-level routes inherit the radix-tree
builder and `default_response_class` plumbing for free. Registration adds the
blueprint's collected routes to the app's own tree under the combined prefix,
and the blueprint keeps them cached so it can be registered on several apps.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from typing_extensions import Doc

from veloce._internal import T_route, _readd_route
from veloce.exceptions import _error_handler_key_error
from veloce.routing.router import RouteInfo, Router

#: The key a scoped handler table is indexed by - an exception class for
#: `_exception_handlers`, a status code for `_status_handlers`.
_HandlerKey = TypeVar("_HandlerKey")


#: Bound once so the route-name check reads the argument by name: `Router.route`
#: takes `name` positionally among some forty options, and an index would move.
_ROUTE_SIGNATURE = inspect.signature(Router.route)


def _reject_dotted(name: str | None, label: str) -> None:
    """Refuse a name carrying the character that separates endpoint segments.

    An endpoint is `"{blueprint}.{route}"`, so a dot inside either half makes the
    boundary unrecoverable: a route named `"users.list"` reads as belonging to
    the blueprint `"admin.users"`, which nobody registered, and every
    blueprint-scoped hook - an authentication `before_request` included - is
    silently skipped for it. Refused where it is written, because `"a.b.c"`
    describes a nested blueprint's route as well as a dotted name on a flat one.
    """
    if name and "." in name:
        raise ValueError(
            f"{label} {name!r} may not contain a dot: `.` separates the blueprint "
            "from the route in an endpoint, so a dotted name makes the route's "
            "blueprint ambiguous and its hooks unreachable. Use an underscore."
        )


def _endpoint_blueprint(endpoint: str | None) -> str | None:
    """Return the dotted blueprint path encoded in an endpoint, or `None`.

    Blueprint routes have endpoints of the form `"{bp}.{routename}"`, and a
    nested one `"{bp}.{child}.{routename}"`; app-level routes have a bare
    `"routename"` (no dot), the same convention `register_blueprint` and
    `url_for` use.

    Everything before the *last* dot, so a nested route resolves to the
    blueprint it was declared on rather than to its outermost ancestor. The app
    flattens each path's ancestor chain at registration, so this stays one key
    and one lookup per request.
    """
    if not endpoint:
        return None
    dot = endpoint.rfind(".")
    return endpoint[:dot] if dot >= 0 else None


def _merge_scoped(
    dst: dict[str, dict[_HandlerKey, Callable[..., Any]]],
    child_own: dict[_HandlerKey, Callable[..., Any]],
    child_scoped: dict[str, dict[_HandlerKey, Callable[..., Any]]],
    child_name: str,
) -> None:
    """Merge a child's own + already-scoped handler tables into `dst`.

    The child's own table lands under its bare dotted name; each of the
    child's already-scoped tables keeps its suffix under the child's name.
    """
    if child_own:
        dst[child_name] = dict(child_own)
    for suffix, table in child_scoped.items():
        dst[f"{child_name}.{suffix}"] = table


#: Every category that must stay scoped to the blueprint it was declared on, as
#: `(own attribute, scoped attribute)`; a plain `extend` would widen its scope.
_SCOPED_LIST_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("_before_request_hooks", "_scoped_before_hooks"),
    ("_after_request_hooks", "_scoped_after_hooks"),
    ("_teardown_request_hooks", "_scoped_teardown_hooks"),
    ("_url_value_preprocessors", "_scoped_url_value_preprocessors"),
    ("_url_default_funcs", "_scoped_url_default_funcs"),
)


def _merge_scoped_lists(
    dst: dict[str, list[Callable[..., Any]]],
    child_own: list[Callable[..., Any]],
    child_scoped: dict[str, list[Callable[..., Any]]],
    child_name: str,
) -> None:
    """Merge a child's own + already-scoped callable lists into `dst`.

    The list counterpart of `_merge_scoped`: the child's own callables land
    under its bare name, each already-scoped descendant keeps its suffix.

    The child's entry is written even when it declares nothing of this category,
    because `dst` is what tells registration which descendant paths exist - with
    no `<parent>.<child>` path, the parent's own hooks have nowhere to be
    flattened onto and never run on the child's routes.
    """
    dst[child_name] = list(child_own)
    for suffix, entries in child_scoped.items():
        dst[f"{child_name}.{suffix}"] = entries


def _resolve_scoped_chain(
    own: list[Callable[..., Any]], scoped: dict[str, list[Callable[..., Any]]], suffix: str
) -> list[Callable[..., Any]]:
    """Flatten the callables that apply to a descendant, outermost first.

    A route under `<bp>.<child>.<grandchild>` runs the blueprint's hooks, then
    the child's, then the grandchild's. Flattening at registration keeps the
    per-request path one dict lookup rather than a walk up the parent chain.
    """
    chain = list(own)
    parts = suffix.split(".")
    for depth in range(len(parts)):
        chain.extend(scoped.get(".".join(parts[: depth + 1]), ()))
    return chain


class Blueprint(Router):
    """Deferred-registration route collection.

    Usage::

        from veloce import Blueprint, Veloce

        bp = Blueprint("admin", url_prefix="/admin")

        @bp.get("/ping")
        async def ping():
            return {"ok": True}

        app = Veloce()
        app.register_blueprint(bp)  # serves GET /admin/ping
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc("Blueprint name, used to prefix endpoint names for `url_for` (`<name>.<route>`)."),
        ],
        url_prefix: Annotated[
            str,
            Doc("Path prefix prepended to every route, overridable at `register_blueprint` time."),
        ] = "",
        default_response_class: Annotated[
            Any,
            Doc(
                "Response class used for blueprint routes that do not declare their own `response_class`."
            ),
        ] = None,
        dependencies: Annotated[
            list[Any] | None,
            Doc(
                "Dependencies applied to every route on this blueprint, run before per-route ones."
            ),
        ] = None,
        responses: Annotated[
            dict[int, dict[str, Any]] | None,
            Doc("Additional OpenAPI responses overlaid onto every route on this blueprint."),
        ] = None,
        tags: Annotated[
            list[str] | None,
            Doc("OpenAPI tags applied to every route registered on this blueprint."),
        ] = None,
        on_duplicate: Annotated[
            str,
            Doc(
                "Policy for a second handler on the same path and method: `error`, `warn`, or `override`."
            ),
        ] = "error",
    ) -> None:
        super().__init__(
            prefix=url_prefix,
            default_response_class=default_response_class,
            dependencies=dependencies,
            responses=responses,
            tags=tags,
            on_duplicate=on_duplicate,
        )
        _reject_dotted(name, "Blueprint name")
        self.name = name
        self.url_prefix = url_prefix
        # Pending registrations, applied to the app at `register_blueprint` time.
        self._before_request_hooks: list[Callable[..., Any]] = []
        self._after_request_hooks: list[Callable[..., Any]] = []
        self._teardown_request_hooks: list[Callable[..., Any]] = []
        self._exception_handlers: dict[type[Any], Callable[..., Any]] = {}
        self._status_handlers: dict[int, Callable[..., Any]] = {}
        # Under each child's dotted suffix, so one child cannot leak to a sibling.
        self._scoped_exception_handlers: dict[str, dict[type[Any], Callable[..., Any]]] = {}
        self._scoped_status_handlers: dict[str, dict[int, Callable[..., Any]]] = {}
        # URL processors, gated to blueprint endpoints at register time.
        self._url_value_preprocessors: list[Callable[..., Any]] = []
        self._url_default_funcs: list[Callable[..., Any]] = []
        # A nested child's hooks and URL processors, scoped under its dotted
        # suffix; each entry holds only that descendant's own callables.
        self._scoped_before_hooks: dict[str, list[Callable[..., Any]]] = {}
        self._scoped_after_hooks: dict[str, list[Callable[..., Any]]] = {}
        self._scoped_teardown_hooks: dict[str, list[Callable[..., Any]]] = {}
        self._scoped_url_value_preprocessors: dict[str, list[Callable[..., Any]]] = {}
        self._scoped_url_default_funcs: dict[str, list[Callable[..., Any]]] = {}

    # ── Hook decorators ───────────────────────────────────

    def route(self, *args: Any, **kwargs: Any) -> Any:
        """Declare a route, refusing a name that would break the endpoint split.

        On the decorator rather than on `add_route`, because every user-facing
        shortcut (`get`, `post`, ...) reaches `route` while `_readd_route` calls
        `add_route` with an endpoint it has *already* composed - `"child.handler"`
        - which this would otherwise reject.
        """
        bound = _ROUTE_SIGNATURE.bind_partial(self, *args, **kwargs)
        _reject_dotted(bound.arguments.get("name"), "Blueprint route name")
        return super().route(*args, **kwargs)

    def before_request(self, func: T_route) -> T_route:
        """Register a function to run before each blueprint request.

        Fires only for requests that match a route declared on this
        blueprint. Use `app.before_request` for app-wide hooks.
        """
        self._before_request_hooks.append(func)
        return func

    def after_request(self, func: T_route) -> T_route:
        """Register a function to run after each blueprint request."""
        self._after_request_hooks.append(func)
        return func

    def teardown_request(self, func: T_route) -> T_route:
        """Run after blueprint-routed request teardown, with optional exc."""
        self._teardown_request_hooks.append(func)
        return func

    def errorhandler(self, exc_class_or_status: type[Any] | int) -> Callable[[T_route], T_route]:
        """Blueprint-scoped error handler.

        Matches `app.errorhandler` semantics: integer keys go to the status-code
        table, classes to the MRO-matched exception table. The handler runs for
        exceptions raised by this blueprint's handlers, with app-level handlers
        as fallback.
        """

        def decorator(func: T_route) -> T_route:
            if isinstance(exc_class_or_status, int):
                self._status_handlers[exc_class_or_status] = func
            else:
                # The exception table is matched by an MRO walk, so a key that
                # is not an exception class could never be found.
                if not (
                    isinstance(exc_class_or_status, type)
                    and issubclass(exc_class_or_status, BaseException)
                ):
                    raise TypeError(_error_handler_key_error(exc_class_or_status))
                self._exception_handlers[exc_class_or_status] = func
            return func

        return decorator

    exception_handler = errorhandler

    def url_value_preprocessor(self, func: T_route) -> T_route:
        """Register a `fn(endpoint, values)` URL preprocessor on this blueprint.

        Mirrors `@app.url_value_preprocessor` - runs after route match for
        blueprint-routed requests, mutating `values` in place.
        """
        self._url_value_preprocessors.append(func)
        return func

    def url_defaults(self, func: T_route) -> T_route:
        """Register a `fn(endpoint, values)` URL-defaults injector for `url_for`.

        Mirrors `@app.url_defaults` - runs inside `url_for` / `url_path_for` for
        endpoints belonging to this blueprint. Use `values.setdefault(...)` for
        caller-wins semantics.
        """
        self._url_default_funcs.append(func)
        return func

    # ── Nested blueprints ─────────────────────────────────

    def register_blueprint(
        self,
        child: Blueprint,
        url_prefix: str | None = None,
    ) -> None:
        """Mount another blueprint as a sub-blueprint of this one.

        Routes from `child` register under
        `self.url_prefix + (url_prefix or child.url_prefix) + path`; endpoint
        names become `<child.name>.<handler>` and pick up the `<self.name>.`
        prefix once *this* blueprint is registered with an app, yielding a final
        `<self.name>.<child.name>.<handler>`.

        Hooks and error handlers from `child` are merged into this blueprint's
        tables, not the app's - the app gets them when *this* one is registered.
        """
        if child is self:
            raise ValueError(f"Cannot register blueprint {self.name!r} as a child of itself.")
        nested_prefix = url_prefix if url_prefix is not None else child.url_prefix
        for path, methods, info in child._walk_routes():
            full_path = (nested_prefix or "") + path
            endpoint = f"{child.name}.{info.name}"
            _readd_route(self, full_path, methods, info, endpoint)

        # Scoped to the child under its dotted name: merged into this
        # blueprint's lists, a `@child.before_request` would run on a sibling.
        for own_attr, scoped_attr in _SCOPED_LIST_CATEGORIES:
            _merge_scoped_lists(
                getattr(self, scoped_attr),
                getattr(child, own_attr),
                getattr(child, scoped_attr),
                child.name,
            )
        # Scoped under the child's dotted name, so a `@child.errorhandler`
        # catches the child's routes and never a sibling's.
        _merge_scoped(
            self._scoped_exception_handlers,
            child._exception_handlers,
            child._scoped_exception_handlers,
            child.name,
        )
        _merge_scoped(
            self._scoped_status_handlers,
            child._status_handlers,
            child._scoped_status_handlers,
            child.name,
        )

    # ── Route collection inspection - used by register_blueprint ──

    def _walk_routes(self) -> list[tuple[str, list[str], RouteInfo]]:
        """Return `(path, methods, RouteInfo)` triples for every route.

        The blueprint's own `url_prefix` is **stripped** from each path before
        returning, so `register_blueprint` can re-apply a (possibly different)
        prefix without double-nesting; the caller prepends the chosen prefix.
        """
        results: list[tuple[str, list[str], RouteInfo]] = []
        own_prefix = self.url_prefix.rstrip("/")
        # include_hidden=True: a blueprint's WebSocket routes and its
        # include_in_schema=False routes must still enter the app's tree.
        for method, _path, info in self._collect_all_routes(include_hidden=True):
            # `path_template`, not the radix-reconstructed path, which loses the
            # trailing-slash distinction the re-prefix step below must preserve.
            full_path = info.path_template
            if own_prefix and full_path.startswith(own_prefix):
                # Stripped verbatim, preserving an empty and an explicit `/`
                # remainder as their own distinct shapes.
                stripped = full_path[len(own_prefix) :]
            else:
                stripped = full_path
            results.append((stripped, [method], info))
        return results
