"""`serving/` holds no module-level import of the app layer.

The repository invariant (CLAUDE.md, "WebSockets work on both paths") is that
the serving layer reaches the application only through the reference it is
handed - `HttpProtocol(app, loop)` - and never by importing `veloce.app` at
module scope. That is what lets `app/` import `serving/` for `Veloce.run()`
without closing an import cycle, and it is why `protocol.py` names `Veloce`
only under `TYPE_CHECKING`.

Until now the claim was stated in the docstring of a test-owned protocol driver
that stood up its own localhost server. A driver written to obey the rule is
not a check that the rule holds; the source is. This scans it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import veloce

_SERVING = pathlib.Path(veloce.__file__).parent / "serving"
_MODULES = sorted(_SERVING.glob("*.py"))


def _runtime_app_imports(source: str) -> list[str]:
    """Module-level imports of the app layer, excluding `TYPE_CHECKING` blocks.

    Only the module body is walked, so a deferred import inside a function is
    out of scope by construction - it costs nothing at import time and cannot
    close a cycle. `test_deferred_import_discipline.py` covers those.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[:2] == ["veloce", "app"]:
                found.append(node.module)
        elif isinstance(node, ast.Import):
            found.extend(
                alias.name for alias in node.names if alias.name.split(".")[:2] == ["veloce", "app"]
            )
    return found


@pytest.mark.parametrize("path", _MODULES, ids=lambda p: p.name)
def test_a_serving_module_does_not_import_the_app_layer(path: pathlib.Path):
    offenders = _runtime_app_imports(path.read_text(encoding="utf-8"))
    assert not offenders, (
        f"{path.name} imports {offenders} at module scope. The serving layer "
        "uses the `app` reference it is handed; name `Veloce` under "
        "`TYPE_CHECKING` if you need the annotation."
    )


def test_the_scan_covers_the_serving_package():
    assert _SERVING.is_dir()
    assert {p.name for p in _MODULES} >= {"__init__.py", "protocol.py"}


def test_a_module_scope_import_would_be_found():
    assert _runtime_app_imports("from veloce.app import Veloce") == ["veloce.app"]
    assert _runtime_app_imports("import veloce.app.asgi") == ["veloce.app.asgi"]


def test_a_type_checking_import_is_not_an_offence():
    source = (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from veloce.app import Veloce\n"
    )
    assert _runtime_app_imports(source) == []


def test_a_deferred_import_is_not_an_offence():
    source = "def build():\n    from veloce.app import Veloce\n    return Veloce\n"
    assert _runtime_app_imports(source) == []


def test_a_sibling_veloce_import_is_not_an_offence():
    assert _runtime_app_imports("from veloce.websocket import WebSocket") == []
