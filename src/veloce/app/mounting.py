"""Mounting — sub-application and static mounts, mixed into Veloce.

Registration is setup-only; the per-request match is a small prefix scan over
the registered mounts.
"""

from __future__ import annotations

from typing import Annotated, Any

from typing_extensions import Doc

from veloce.app._host import AppHost


class MountingMixin(AppHost):
    """Sub-app / ASGI / static mounting, mixed into `Veloce`."""

    def mount(
        self,
        prefix: Annotated[str, Doc("Full path the sub-application is served at.")],
        app: Annotated[Any, Doc("A `Veloce` sub-app, an ASGI application, or a `StaticFiles`.")],
        *,
        expose_mcp: Annotated[
            bool, Doc("Also publish the sub-app's MCP tools through this app's server.")
        ] = False,
    ) -> None:
        """Mount a sub-application at a path prefix.

        `prefix` is the full path the sub-app is reached at; this app's own
        `Veloce(prefix=...)` does not apply here, since a mount places an application
        rather than registering a route.

        A Veloce sub-app is dispatched through the parent's request pipeline, and
        the parent drives its startup and shutdown. Any other ASGI app is
        dispatched at the ASGI layer with the prefix moved from `path` to
        `root_path`, receives `http` and `websocket` scopes only - never
        `lifespan` - and owns its entire prefix subtree.

        Registering a prefix equal to, nested under, or containing an existing
        mount raises `ValueError`.

        `expose_mcp=True` also publishes the sub-app's MCP tools, resources and
        prompts through the parent's server, prefixed by the mount.
        """
        # `mount` is a setup-time call, never on the request path.
        from veloce.app.core import Veloce  # breaks app.mounting->app.core cycle

        prefix = prefix.rstrip("/")
        # A request path always starts with "/", so a prefix given without one
        # is normalised rather than left silently unreachable.
        prefix = self._reject_overlapping_prefix(prefix)
        entry = (prefix, prefix + "/", app)
        if isinstance(app, Veloce):
            self._register_feature_state(self._mounted_apps, entry)
            if expose_mcp:
                # Opt-in: mounting an app for its HTTP routes must not silently
                # hand an agent everything it can do.
                self._mcp_mounts.append((prefix, app))
            return
        # A Veloce-protocol handler - `.prefix` plus awaitable `handle(request)`.
        # `StaticFiles` looks ASGI-shaped but is not, and gating on the protocol
        # rather than the class serves a user's own handler the same way.
        if hasattr(app, "prefix") and callable(getattr(app, "handle", None)):
            app.prefix = prefix
            self._register_feature_state(self._static_handlers, app)
            return
        # Catch a non-callable at registration instead of as a per-request 500.
        if not callable(app):
            raise TypeError(
                f"mount({prefix or '/'!r}, ...) expected an ASGI application "
                f"(callable taking `(scope, receive, send)`), a `Veloce` sub-app, "
                f"or a `StaticFiles` instance - got "
                f"{type(app).__name__} which is none of those. "
                f"For Veloce's own static-file handler, prefer "
                f"`app.mount_static(prefix=..., directory=...)`."
            )
        self._register_feature_state(self._asgi_mounts, entry)

    def mount_static(
        self,
        prefix: Annotated[str, Doc("URL path the directory is served under.")] = "/static",
        directory: Annotated[
            str, Doc("Directory to serve, absolute or relative to the package root.")
        ] = "static",
        html: Annotated[
            bool, Doc("Serve `index.html` for a directory request, in HTML mode.")
        ] = False,
        must_exist: Annotated[
            bool, Doc("Refuse a missing directory; `False` downgrades it to a warning.")
        ] = True,
    ) -> None:
        """Mount a static file directory.

        The directory must exist and be readable at wiring time (a typo
        otherwise 404s every asset silently); pass ``must_exist=False`` to
        downgrade the check to a warning when the directory is created after
        the app is constructed.
        """
        # Deferred to keep the layering: `app/` is core, `contrib/` is not.
        from veloce.contrib.staticfiles import StaticFiles

        prefix = self._reject_overlapping_prefix(prefix)
        self._register_feature_state(
            self._static_handlers,
            StaticFiles(directory=directory, prefix=prefix, html=html, must_exist=must_exist),
        )

    def _match_asgi_mount(self, path: str) -> tuple[str, Any] | None:
        """Return the `(prefix, app)` whose prefix owns `path`, if any."""
        for prefix, prefix_slash, mounted in self._asgi_mounts:
            if path == prefix or path.startswith(prefix_slash):
                return prefix, mounted
        return None

    def _path_under_mount(self, path: str) -> bool:
        """Whether `path` is served by a mounted sub-application.

        Asked by the ASGI transport before it drains the body: a mounted path
        matches no route here, so a `stream=True` route in the sub-app would
        otherwise be drained into a buffered one.
        """
        for prefix, prefix_slash, _sub_app in self._mounted_apps:
            if path.startswith(prefix_slash) or path == prefix:
                return True
        return False

    def _reject_overlapping_prefix(self, prefix: str) -> str:
        """Normalise `prefix` and refuse it if it overlaps a registered mount.

        Two prefixes overlap when they are equal or one is a path-segment
        ancestor of the other; mounts are tried in a fixed order, so an overlap
        means one silently shadows the other. Registration-time only.
        """
        if prefix and not prefix.startswith("/"):
            prefix = "/" + prefix
        registered = [
            *(existing for existing, _slash, _app in self._mounted_apps),
            *(existing for existing, _slash, _app in self._asgi_mounts),
            *(handler.prefix for handler in self._static_handlers),
        ]
        for existing in registered:
            if (
                prefix == existing
                or prefix.startswith(existing + "/")
                or existing.startswith(prefix + "/")
            ):
                raise ValueError(
                    f"mount prefix {prefix or '/'!r} overlaps the "
                    f"already-mounted prefix {existing or '/'!r}"
                )
        return prefix
