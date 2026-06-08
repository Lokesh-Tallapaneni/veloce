"""Application package gateway.

The implementation lives in `veloce.app.core` and the focused sibling modules
(`contexts`, `serving`, `templating`, `background`, `urls`). This gateway
re-exports the full `veloce.app` surface so every `from veloce.app import X`
path keeps resolving unchanged.
"""

from __future__ import annotations

from veloce.app.core import *  # noqa: F401,F403
from veloce.app.core import (  # noqa: F401  - private names reached as veloce.app._X
    _exc_handler_sig_cache,
    _URLMap,
)
