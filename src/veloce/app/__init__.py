"""Application package — gateway preserving the `veloce.app` import paths.

The implementation is `veloce.app.core` - the `Veloce` class and its
construction - plus sixteen focused sibling modules mixed into it: `asgi`,
`background`, `contexts`, `dispatch`, `errors`, `introspection`, `lifecycle`,
`mcp`, `middleware`, `mounting`, `openapi`, `plugins`, `serving`, `templating`,
`testing` and `urls`.

This gateway re-exports the `veloce.app` surface by name, so every
`from veloce.app import X` path keeps resolving unchanged. The list above used
to name five of them as though it were complete, which is how it read after the
split it describes added the rest.

None of the mixins declares `__slots__`. `Veloce` is deliberately unslotted -
an application sets its own attributes on it - so a mixin's `__slots__ = ()`
would have no effect, and most of them could not declare one anyway: they write
the host's state, which a slotted class refuses.

The private names below are re-exported deliberately: tests and internal
modules reach them through the module path, so an internal split must not move
them out from under those callers.
"""

from __future__ import annotations

from veloce.app.core import Veloce
from veloce.app.dispatch import (
    _exc_handler_sig_cache,  # noqa: F401  - veloce.app._exc_handler_sig_cache
)
from veloce.app.plugins import Plugin
from veloce.app.urls import URLRule, _URLMap  # noqa: F401  - veloce.app._URLMap

__all__ = ["Plugin", "URLRule", "Veloce"]
