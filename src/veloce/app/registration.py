"""Blueprint and URL-rule registration for `Veloce`."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, cast

from typing_extensions import Doc

from veloce._internal import (
    _readd_route,
)
from veloce._protocol_constants import (
    HTTP_METHOD_GET,
)
from veloce.app._host import AppHost
from veloce.blueprints import Blueprint, _resolve_scoped_chain
from veloce.http.request import Request

if TYPE_CHECKING:  # pragma: no cover
    from veloce.routing.router import Router

    pass


class RegistrationMixin(AppHost):
    """Blueprint registration and URL rules."""

    def register_blueprint(
        self,
        blueprint: Annotated[Blueprint, Doc("Blueprint whose routes and hooks are mounted.")],
        url_prefix: Annotated[
            str | None, Doc("Path prefix to mount under; defaults to the blueprint's own.")
        ] = None,
    ) -> None:
        """Mount a `Blueprint`'s routes + hooks onto this app.

        Each route is re-registered under `(url_prefix or bp.url_prefix) + path`,
        so the same blueprint can be mounted twice. Its request hooks and error
        handlers are bucketed under its name - a nested child under its dotted
        name - so they fire only for that blueprint's own routes, with an
        app-level error handler still the fallback. The blueprint itself is left
        unmodified, so it can be mounted on several apps.
        """
        self._assert_mutable()

        if not isinstance(blueprint, Blueprint):
            raise TypeError(
                f"register_blueprint expects a Blueprint, got {type(blueprint).__name__}"
            )

        effective_prefix = url_prefix if url_prefix is not None else blueprint.url_prefix
        bp_name = blueprint.name
        # Identity, not name: re-mounting *this* blueprint must not append its
        # hooks twice, while a namesake would collide on `<name>.`.
        existing = self._blueprints_map.get(bp_name)
        if existing is not None and existing is not blueprint:
            raise ValueError(
                f"a different blueprint named {bp_name!r} is already registered: both would "
                f"share the {bp_name!r} endpoint prefix, so their routes and hooks cannot be "
                "told apart. Give one a different name."
            )
        already_registered = existing is not None
        self._blueprints_map[bp_name] = blueprint

        # `<bpname>.` prefixes the name so `url_for` and hook-gating can find it.
        for path, methods, info in blueprint._walk_routes():
            full_path = (effective_prefix or "") + path
            endpoint = f"{bp_name}.{info.name}"
            _readd_route(cast("Router", self), full_path, methods, info, endpoint)

        # Bucketed by name so dispatch looks hooks up by the route's endpoint
        # prefix. The routes above are per-mount, these per-name.
        for own_attr, scoped_attr, bucket in (
            ()
            if already_registered
            else (
                ("_before_request_hooks", "_scoped_before_hooks", self._bp_before_hooks),
                ("_after_request_hooks", "_scoped_after_hooks", self._bp_after_hooks),
                ("_teardown_request_hooks", "_scoped_teardown_hooks", self._bp_teardown_hooks),
                (
                    "_url_value_preprocessors",
                    "_scoped_url_value_preprocessors",
                    self._bp_url_value_preprocessors,
                ),
                ("_url_default_funcs", "_scoped_url_default_funcs", self._bp_url_default_funcs),
            )
        ):
            own = getattr(blueprint, own_attr)
            scoped = getattr(blueprint, scoped_attr)
            if own:
                bucket.setdefault(bp_name, []).extend(own)
            for suffix in scoped:
                chain = _resolve_scoped_chain(own, scoped, suffix)
                if chain:
                    bucket.setdefault(f"{bp_name}.{suffix}", []).extend(chain)
            if own or scoped:
                # Keeps `is_bare` fresh for a blueprint with hooks but no routes.
                self._gen += 1

        # Out of the app-global tables, so only this blueprint's own.
        if blueprint._exception_handlers:
            self._bp_exception_handlers.setdefault(bp_name, {}).update(
                blueprint._exception_handlers
            )
        if blueprint._status_handlers:
            self._bp_status_handlers.setdefault(bp_name, {}).update(blueprint._status_handlers)
        for suffix, table in blueprint._scoped_exception_handlers.items():
            self._bp_exception_handlers.setdefault(f"{bp_name}.{suffix}", {}).update(table)
        for suffix, status_table in blueprint._scoped_status_handlers.items():
            self._bp_status_handlers.setdefault(f"{bp_name}.{suffix}", {}).update(status_table)

    def add_url_rule(
        self,
        rule: Annotated[
            str,
            Doc("URL path template, including `{param}` / `{param:converter}` placeholders."),
        ],
        endpoint: Annotated[
            str | None,
            Doc("Endpoint name for `url_for`; required when registering an endpoint-only stub."),
        ] = None,
        view_func: Annotated[
            Callable[..., Any] | None,
            Doc(
                "Handler for the route; `None` registers an endpoint-only stub for later attachment."
            ),
        ] = None,
        methods: Annotated[
            list[str] | None,
            Doc("HTTP methods this rule serves; defaults to `GET`."),
        ] = None,
        **kwargs: Any,
    ) -> None:
        """Add a URL rule programmatically.

        `view_func=None` registers an **endpoint-only stub**: the route resolves
        for `url_for` but raises `RuntimeError` until `@app.endpoint(endpoint)`
        attaches a handler. `endpoint` is required in that case.
        """
        if view_func is None:
            if not endpoint:
                raise ValueError(
                    "add_url_rule needs `endpoint` when `view_func` is None (endpoint-only stub)"
                )

            async def _stub_view(request: Request, **path_params: Any) -> Any:
                raise RuntimeError(
                    f"endpoint {endpoint!r} has no view function yet - "
                    f"attach one with @app.endpoint({endpoint!r})"
                )

            _stub_view.__name__ = endpoint
            view_func = _stub_view
        cast("Router", self).add_route(
            path=rule,
            handler=view_func,
            # `View.as_view` sets `view.methods`, so all of them register.
            methods=methods or getattr(view_func, "methods", None) or [HTTP_METHOD_GET],
            name=endpoint,
            **kwargs,
        )
