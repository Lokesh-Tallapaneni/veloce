"""Application package — gateway preserving the `veloce.app` import paths.

`Veloce` is `veloce.app.core` plus sixteen focused sibling modules mixed into
it: `asgi`, `background`, `contexts`, `dispatch`, `errors`, `introspection`,
`lifecycle`, `mcp`, `middleware`, `mounting`, `openapi`, `plugins`, `serving`,
`templating`, `testing` and `urls`. All are re-exported here by name, so every
`from veloce.app import X` path keeps resolving unchanged.

No mixin declares `__slots__`: `Veloce` is deliberately unslotted, and most
mixins write the host's state, which a slotted class refuses.

The private name below is re-exported deliberately - tests and internal modules
reach it through the module path.
"""

from __future__ import annotations

from veloce.app.core import Veloce
from veloce.app.dispatch import (
    _exc_handler_sig_cache as _exc_handler_sig_cache,  # veloce.app._exc_handler_sig_cache
)
from veloce.app.plugins import Plugin
from veloce.app.urls import URLMap, URLRule

__all__ = ["Plugin", "URLMap", "URLRule", "Veloce"]
