"""Application package — gateway preserving the `veloce.app` import paths.

The implementation lives in `veloce.app.core`, `veloce.app.dispatch`, and the
focused sibling modules (`contexts`, `serving`, `templating`, `background`,
`urls`). This gateway re-exports the `veloce.app` surface by name so every
`from veloce.app import X` path keeps resolving unchanged.

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
