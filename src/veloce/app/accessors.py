"""Accessors for `Veloce` — JSON provider, static files, paths, signals, CLI and dependency overrides."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any

from veloce.app._host import AppHost
from veloce.helpers import Aborter, send_from_directory, send_from_directory_async


class AccessorsMixin(AppHost):
    """Convenience accessors and properties on the application."""

    @property
    def json(self) -> Any:
        """Active `JSONProvider` instance.

        Lazily instantiated from `app.json_provider_class`, so swapping encoders
        is `app.json_provider_class = MyJSONProvider`; `app.json = instance`
        replaces it explicitly.

        The provider serialises every value a handler returns, and an app that
        configures nothing keeps the direct backend path. It does not reach the
        framework's own wire formats: cache keys always sort so equal mappings
        hash alike, and signed cookies, JWTs and protocol frames are not the
        application's to restyle.
        """
        if self._json_provider is None:
            self._json_provider = self.json_provider_class(self)
        return self._json_provider

    @json.setter
    def json(self, provider: Any) -> None:
        self._json_provider = provider

    def _resolve_handler_json_dumps(self) -> Any:
        """Resolve the serialiser a handler's JSON return must use, or `None`.

        `None` when the active provider is the stock one with no configured
        options, which the direct path already matches. Resolved once, so set
        the options before the first request.
        """
        # Deferred: `json_provider` reaches back into the app package.
        from veloce.json_provider import resolve_dumps

        return resolve_dumps(self)

    def send_static_file(self, filename: str) -> Any:
        """Serve a file from `app.static_folder`.

        `app.static_folder` defaults to `"static"`, relative to
        `app.package_root`. Returns a `FileResponse`, traversal-safe via
        `safe_join`. Reads synchronously and warns on a running loop, so async
        handlers should prefer `send_static_file_async`.
        """
        directory = self.static_folder
        if not os.path.isabs(directory):
            directory = os.path.join(self.package_root, directory)
        return send_from_directory(directory, filename)

    async def send_static_file_async(self, filename: str) -> Any:
        """Serve a file from `app.static_folder` - async variant.

        Reads the file in an executor, so it never blocks the event loop.
        """
        directory = self.static_folder
        if not os.path.isabs(directory):
            directory = os.path.join(self.package_root, directory)
        return await send_from_directory_async(directory, filename)

    @property
    def package_root(self) -> str:
        """Filesystem path of the directory containing `import_name`'s module.

        Named `package_root` rather than `root_path` because `Veloce.root_path`
        already means the ASGI mount prefix - one is a location on disk, the
        other a URL prefix.
        """
        mod = sys.modules.get(self.import_name)
        mod_file = getattr(mod, "__file__", None) if mod else None
        if mod_file:
            return os.path.dirname(os.path.abspath(mod_file))
        return os.getcwd()

    @property
    def instance_path(self) -> str:
        """Writable instance folder beside the package.

        `<package_root>/instance`, for config, SQLite files and uploads, unless
        an explicit `instance_path=` overrides it. *Not* auto-created.
        """
        if self._instance_path is not None:
            return self._instance_path
        return os.path.join(self.package_root, "instance")

    @property
    def signal_namespace(self) -> Any:
        """Accessor that returns the `veloce.signals` module.

        The signals are module-level singletons, so
        `app.signal_namespace.request_started` *is* `veloce.signals.request_started`.
        """
        from veloce import signals

        return signals

    @property
    def aborter(self) -> Any:
        """Callable that raises typed `HTTPException`s by status code.

        `app.aborter(404)` is the module-level `abort(404)`. A distinct attribute
        so an application can subclass `Aborter` for custom code-to-exception
        mappings, by assignment or by mutating the instance this returns. One per
        application, so a mutation sticks and reaches no other app.
        """
        if self._aborter is None:
            self._aborter = Aborter()
        return self._aborter

    @aborter.setter
    def aborter(self, value: Any) -> None:
        self._aborter = value

    @property
    def got_first_request(self) -> bool:
        """`True` after the first request has been fully handled.

        Read-only, for conditional setup that depends on whether the app has
        bootstrapped yet.
        """
        return self._first_request_fired

    @property
    def cli(self) -> Any:
        """Click `Group` for app-defined custom CLI commands.

        Lazily constructs a `click.Group` once. Custom commands attach via the
        standard Click decorator:

            @app.cli.command("init-db")
            def init_db():
                ...

        The `veloce` console script mounts the group as a `custom` subcommand
        when launched with an app reference. `click` is required at access time
        but not at import time.
        """
        if self._cli_group is None:
            try:
                import click
            except ImportError as err:  # pragma: no cover
                raise RuntimeError(
                    "app.cli requires `click`. Install it with: pip install veloceframework[cli]"
                ) from err
            self._cli_group = click.Group(
                name=self.title.lower().replace(" ", "-"),
                help=f"Custom CLI for {self.title}.",
            )
        return self._cli_group

    def test_cli_runner(self, **kwargs: Any) -> Any:
        """Return a Click `CliRunner` bound for testing `app.cli`.

        Kwargs flow through to `click.testing.CliRunner`.
        """
        try:
            from click.testing import CliRunner
        except ImportError as err:  # pragma: no cover
            raise RuntimeError(
                "test_cli_runner() requires `click`. Install it with: pip install veloceframework[cli]"
            ) from err
        return CliRunner(**kwargs)

    def dependency_overrides_provider(self) -> dict[Callable[..., Any], Callable[..., Any]]:
        """Return the dependency override mapping."""
        return self._dependency_overrides

    @property
    def dependency_overrides(self) -> dict[Callable[..., Any], Callable[..., Any]]:
        """Mutable map of dependency callables to test replacements.

        Populate it to swap a real dependency for a fake one in tests::

            app.dependency_overrides[get_db] = get_fake_db

        The resolver consults it on every request, so a change takes effect
        immediately, and `.clear()` removes every override.
        """
        return self._dependency_overrides

    @dependency_overrides.setter
    def dependency_overrides(self, value: dict[Callable[..., Any], Callable[..., Any]]) -> None:
        self._dependency_overrides = value
        # The previous callables' sub-plans are unreachable but still pin them.
        self._override_subplans.clear()
