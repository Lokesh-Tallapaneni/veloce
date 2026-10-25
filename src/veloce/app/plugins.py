"""Plugin protocol — one-call registration of an app extension, mixed into Veloce.

A plugin is any object exposing `install(self, app)`; an optional `name`
attribute makes it reachable via `app.extensions`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from veloce.app._host import AppHost

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app.core import Veloce


@runtime_checkable
class Plugin(Protocol):
    """A Veloce plugin: any object exposing ``install(self, app)``.

    Usage::

        class TimingPlugin:
            name = "timing"

            def install(self, app):
                app.add_instrumentation(self._record)

        app.install(TimingPlugin())
    """

    def install(self, app: Veloce) -> None: ...


class PluginsMixin(AppHost):
    """`Veloce.install()` - register an extension in one call."""

    def install(self, plugin: Plugin) -> Plugin:
        """Install ``plugin`` and return it.

        A named plugin is recorded under ``self.extensions[name]`` only after its
        ``install`` returns, so a failed install leaves no partial registry entry.
        """
        installer = getattr(plugin, "install", None)
        if not callable(installer):
            raise TypeError(
                f"plugin must expose a callable install(app); got {plugin!r} "
                "(did you pass a class or bare function instead of a plugin instance?)"
            )
        name = getattr(plugin, "name", None)
        if name and name in self.extensions:
            raise ValueError(f"a plugin named {name!r} is already installed")
        installer(self)
        if name:
            self.extensions[name] = plugin
        return plugin
