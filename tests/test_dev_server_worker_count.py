"""`Veloce.run` refuses a worker count the built-in server cannot honour.

The native dev server is single-process, so `run(workers=4)` is a configuration
mistake that must fail loudly rather than silently serving one worker. This
lived in `test_debug.py`, whose subject is the HTML traceback page, where
nobody grepping for dev-server coverage would find it.
"""

from __future__ import annotations

import pytest

from veloce import Veloce


def test_run_rejects_multiple_workers():
    app = Veloce()
    with pytest.raises(ValueError, match="runs a single process"):
        app.run(workers=4)
