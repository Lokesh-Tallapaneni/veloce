"""Mounting — sub-application and static mounts, mixed into Veloce.

Holds `mount` (Veloce sub-app, ASGI app, or `StaticFiles` at a prefix),
`mount_static` (the Veloce static-file handler), and the per-request
`_match_asgi_mount` lookup the ASGI dispatcher uses to route a path into a
mounted ASGI app. A mixin on `Veloce`; registration is setup-only and the match
is a small prefix scan over the registered mounts.
"""

from __future__ import annotations

from typing import Any

from veloce.app._host import AppHost


class MountingMixin(AppHost):
    """Sub-app / ASGI / static mounting, mixed into `Veloce`."""

    def _path_under_mount(self, path: str) -> bool:
        """Whether `path` is served by a mounted sub-application.

        Asked by the ASGI transport before it decides to drain the body: a
        mounted path matches no route here, so without this the eager drain runs
        before anything can ask whether the sub-app's route streams, and a
        `stream=True` route silently became a buffered one once mounted. Only
        consulted when the compiled pipeline says mounts exist.
        """
        for prefix, prefix_slash, _sub_app in self._mounted_apps:
            if path.startswith(prefix_slash) or path == prefix:
                return True
        return False

    def _reject_overlapping_prefix(self, prefix: str) -> str:
        """Normalise `prefix` and refuse it if it overlaps a registered mount.

        Two prefixes overlap when one is a path-segment ancestor of the other,
        or they are equal. Mounts are tried in a fixed order, so an overlap means
        one silently shadows the other - and because sub-apps are tried before
        static handlers, a static mount sharing a prefix with an app mount served
        nothing at all, in either registration order.

        All three registries are consulted, so the answer does not depend on
        which entry point registered the other side. Registration-time only: it
        scans a handful of mounts once per `mount` call and nothing per request.
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

    def mount(self, prefix: str, app: Any, *, expose_mcp: bool = False) -> None:
        """Mount a sub-application at a path prefix.

        `prefix` is the full path the sub-app is reached at. The app's own
        `Veloce(prefix=...)` does not apply here - that prepends to routes this
        app *registers*, and a mount places another application rather than
        registering a route. An app built with `Veloce(prefix="/api")` and
        `mount("/sub", child)` serves the child at `/sub`, not `/api/sub`; write
        `mount("/api/sub", child)` for that.

        A veloce sub-app is dispatched through the parent's request
        pipeline. Any other ASGI application - an ASGI micro-app, an
        instrumentation shim - is dispatched at the ASGI layer instead:
        the matched prefix is stripped from the scope's `path` and moved
        onto `root_path`, so the mounted app sees a normal root-relative
        request.

        Lifecycle: a mounted *Veloce* sub-app has its startup and shutdown
        driven by the parent - the parent runs each child's startup after its
        own during `lifespan`/`run()` startup, and tears children down in
        reverse on shutdown, so a child's `on_startup` / lifespan resources
        initialise and release without a separate ASGI lifespan. A mounted
        non-Veloce *ASGI* app receives `http` and `websocket` scopes only:
        the parent does not fan the `lifespan` cycle out to it, so it must
        not depend on ASGI `lifespan` events for its setup. A mounted ASGI
        app owns its entire prefix subtree - a native route registered under
        the same prefix is unreachable.

        Prefixes must not overlap: registering a prefix equal to, nested
        under, or containing an existing mount raises `ValueError`, since
        overlapping mounts would shadow each other in a confusing,
        order-dependent way.

        `expose_mcp=True` additionally publishes the sub-app's MCP tools,
        resources and prompts through the parent's MCP server, with tool and
        prompt names prefixed by the mount. It is opt-in because mounting an app
        for its HTTP routes should not silently hand an agent everything it can
        do.
        """
        # Imported here (not at module top) to break the app.core <-> app.mounting
        # import cycle; `mount` is a setup-time call, never on the request path.
        from veloce.app.core import Veloce

        prefix = prefix.rstrip("/")
        # A request path always starts with "/"; normalise a prefix given
        # without one so the mount is not silently unreachable.
        prefix = self._reject_overlapping_prefix(prefix)
        entry = (prefix, prefix + "/", app)
        if isinstance(app, Veloce):
            self._register_feature_state(self._mounted_apps, entry)
            if expose_mcp:
                # Opt-in: mounting an app must not silently widen the parent's MCP
                # surface, since that would publish tools the parent never chose to
                # expose to an agent.
                self._mcp_mounts.append((prefix, app))
            return
        # A Veloce-protocol handler: `.prefix` plus an awaitable
        # `handle(request)`. `StaticFiles` looks ASGI-shaped - it is an object
        # you would naturally hand to `mount` - but it speaks this protocol, not
        # ASGI, and mounting it as an ASGI app would register successfully and
        # then 500 every request on `await mounted(scope, receive, send)`. Gated
        # on the protocol rather than on the class, so a user's own handler
        # implementing it is served the same way.
        if hasattr(app, "prefix") and callable(getattr(app, "handle", None)):
            app.prefix = prefix.rstrip("/")
            self._register_feature_state(self._static_handlers, app)
            return
        # Anything else must be callable in the ASGI shape. Catching
        # non-callables here surfaces the mistake at registration
        # instead of as a per-request 500 later.
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

    def _match_asgi_mount(self, path: str) -> tuple[str, Any] | None:
        """Return the `(prefix, app)` whose prefix owns `path`, if any."""
        for prefix, prefix_slash, mounted in self._asgi_mounts:
            if path == prefix or path.startswith(prefix_slash):
                return prefix, mounted
        return None

    def mount_static(
        self,
        prefix: str = "/static",
        directory: str = "static",
        html: bool = False,
        must_exist: bool = True,
    ) -> None:
        """Mount a static file directory.

        The directory must exist and be readable at wiring time (a typo
        otherwise 404s every asset silently); pass ``must_exist=False`` to
        downgrade the check to a warning when the directory is created after
        the app is constructed.
        """
        # `app/` is core and `contrib/` is optional, so this is deferred to keep the layering:
        # `contrib/` is optional, so importing it eagerly made every `import
        # veloce` pull in the static-file machinery. Registration-time, once per
        # call - the same shape `app/openapi.py` and `app/mcp.py` already use.
        from veloce.contrib.staticfiles import StaticFiles

        prefix = self._reject_overlapping_prefix(prefix)
        self._register_feature_state(
            self._static_handlers,
            StaticFiles(directory=directory, prefix=prefix, html=html, must_exist=must_exist),
        )
