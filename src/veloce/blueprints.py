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

`Blueprint` extends `Router`, so blueprint-level routes inherit the
radix-tree builder and `default_response_class` plumbing for free.
The registration step splices the blueprint's collected routes into
the app's own tree under the combined prefix; the blueprint instance
keeps the route registrations cached so `register_blueprint` can be
called multiple times on different apps.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from typing_extensions import Doc

from veloce.routing.router import RouteInfo, Router, _readd_route


def _endpoint_blueprint(endpoint: str | None) -> str | None:
    """Return the dotted blueprint path encoded in an endpoint, or `None`.

    Blueprint routes have endpoints of the form `"{bp}.{routename}"`, and a
    nested one `"{bp}.{child}.{routename}"`; app-level routes have a bare
    `"routename"` (no dot). This is the same convention `register_blueprint` and
    `url_for` already use.

    Everything before the *last* dot, so a nested route resolves to the
    blueprint it was declared on rather than to its outermost ancestor - reading
    only the first segment is what made a child's hooks apply to every route
    under the parent, siblings included. The app flattens each path's ancestor
    chain at registration, so this stays one key and one lookup per request.

    Read per request by the app's dispatch and hook paths (`app/dispatch.py`,
    `app/core.py`), so it lives with the convention it decodes rather than being
    restated there.
    """
    if not endpoint:
        return None
    dot = endpoint.rfind(".")
    return endpoint[:dot] if dot >= 0 else None


def _merge_scoped(
    dst: dict[str, dict],
    child_own: dict,
    child_scoped: dict,
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


#: Every registration category that must stay scoped to the blueprint it was
#: declared on. Each row is `(own attribute, scoped attribute)`. A category
#: merged with a plain `extend` instead silently acquires the parent's scope -
#: which is what let a child's `before_request` run on a sibling's routes - so a
#: new category belongs in this table rather than in a hand-written line.
#: Error handlers are merged separately because they are mappings, not lists.
_SCOPED_LIST_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("_before_request_hooks", "_scoped_before_hooks"),
    ("_after_request_hooks", "_scoped_after_hooks"),
    ("_teardown_request_hooks", "_scoped_teardown_hooks"),
    ("_url_value_preprocessors", "_scoped_url_value_preprocessors"),
    ("_url_default_funcs", "_scoped_url_default_funcs"),
)


def _merge_scoped_lists(
    dst: dict[str, list],
    child_own: list,
    child_scoped: dict[str, list],
    child_name: str,
) -> None:
    """Merge a child's own + already-scoped callable lists into `dst`.

    The list counterpart of `_merge_scoped`: the child's own callables land
    under its bare name, each already-scoped descendant keeps its suffix.

    The child's entry is written even when it declares nothing of this
    category, because `dst` is what tells registration which descendant paths
    exist. Recorded only when non-empty, a child that declares no
    `before_request` of its own left no `<parent>.<child>` path at all - and
    the parent's own hooks, which apply to every route beneath it, had nowhere
    to be flattened onto. A guard on the parent then never ran on the child's
    routes.
    """
    dst[child_name] = list(child_own)
    for suffix, entries in child_scoped.items():
        dst[f"{child_name}.{suffix}"] = entries


def _resolve_scoped_chain(own: list, scoped: dict[str, list], suffix: str) -> list:
    """Flatten the callables that apply to a descendant, outermost first.

    A route under `<bp>.<child>.<grandchild>` runs the hooks declared on the
    blueprint, then the child's, then the grandchild's. Flattening the chain at
    registration keeps the per-request path a single dict lookup rather than a
    walk up the endpoint's parent chain.
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
            list | None,
            Doc(
                "Dependencies applied to every route on this blueprint, run before per-route ones."
            ),
        ] = None,
        responses: Annotated[
            dict[int, dict[str, Any]] | None,
            Doc("Additional OpenAPI responses overlaid onto every route on this blueprint."),
        ] = None,
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
        # Nested-child error handlers, kept under each child's dotted name suffix
        # (relative to this blueprint) rather than flattened into the tables above,
        # so a sibling child's handler does not leak across to another child. The
        # app buckets these under `<this_bp_name>.<suffix>` at register time.
        self._scoped_exception_handlers: dict[str, dict[type, Callable]] = {}
        self._scoped_status_handlers: dict[str, dict[int, Callable]] = {}
        # URL processors registered on this blueprint. They're
        # gated to blueprint endpoints at register time.
        self._url_value_preprocessors: list[Callable] = []
        self._url_default_funcs: list[Callable] = []
        # A nested child's hooks and URL processors, kept under the child's
        # dotted suffix for the same reason its error handlers are: merged into
        # the lists above they became this blueprint's own, so a child's
        # `before_request` guard ran on a *sibling* child's routes and a child's
        # `url_value_preprocessor` rewrote a sibling's path params. Each entry
        # holds only that descendant's own callables; the app flattens the chain
        # at register time so one lookup per request still serves them.
        self._scoped_before_hooks: dict[str, list[Callable]] = {}
        self._scoped_after_hooks: dict[str, list[Callable]] = {}
        self._scoped_teardown_hooks: dict[str, list[Callable]] = {}
        self._scoped_url_value_preprocessors: dict[str, list[Callable]] = {}
        self._scoped_url_default_funcs: dict[str, list[Callable]] = {}

    # ── Hook decorators ───────────────────────────────────

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

    # ── Nested blueprints (R4) ────────────────────────────

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
            _readd_route(self, full_path, methods, info, endpoint)

        # Inherit child hooks + error handlers. Child's hooks will be
        # endpoint-gated when *this* blueprint registers onto the app
        # (parent gate covers `<parent_name>.` which matches
        # `<parent_name>.<child_name>....`).
        # Every list category stays scoped to the child under its dotted name,
        # for the same reason the error handlers below do: merged into this
        # blueprint's own lists they become this blueprint's, and a
        # `@child.before_request` guard would then run on a sibling's routes.
        for own_attr, scoped_attr in _SCOPED_LIST_CATEGORIES:
            _merge_scoped_lists(
                getattr(self, scoped_attr),
                getattr(child, own_attr),
                getattr(child, scoped_attr),
                child.name,
            )
        # Error handlers stay scoped to the child (and its descendants) under the
        # child's dotted name, not merged into this blueprint's own tables - so a
        # `@child.errorhandler` only catches the child's routes, never a sibling's.
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
