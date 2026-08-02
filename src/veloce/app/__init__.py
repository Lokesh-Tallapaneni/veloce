"""Application package — gateway preserving the `veloce.app` import paths.

The implementation lives in `veloce.app.core`, `veloce.app.dispatch`, and the
focused sibling modules (`contexts`, `serving`, `templating`, `background`,
`urls`). This gateway re-exports the full `veloce.app` surface so every
`from veloce.app import X` path keeps resolving unchanged.
"""

from __future__ import annotations

from veloce.app.core import *  # noqa: F401,F403
from veloce.app.core import _URLMap  # noqa: F401  - reached as veloce.app._URLMap
from veloce.app.dispatch import (  # noqa: F401  - reached as veloce.app._exc_handler_sig_cache
    _exc_handler_sig_cache,
)
from veloce.app.plugins import Plugin  # noqa: F401  - reached as veloce.app.Plugin
