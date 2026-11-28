"""`import veloce` resolves public names on first use rather than at import.

Eagerly importing the gateway loaded about a hundred modules, pydantic and the
whole router for any process that so much as read `veloce.status`. These tests
pin the properties that matter: a bare import stays small, every advertised
name still resolves, submodules are still reachable as attributes, and every
module can be imported first in a fresh interpreter (a lazy gateway stops
masking import cycles that its eager order used to hide).
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

import veloce

SRC = pathlib.Path(veloce.__file__).resolve().parent.parent

#: A bare `import veloce` may load the gateway and `veloce.status`, nothing else
#: of the framework. A little headroom would let one eager import creep back in.
_BARE_IMPORT_MODULE_BUDGET = 4


def _run(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_a_bare_import_loads_almost_nothing():
    out = _run(
        "import sys, veloce\n"
        "print(len([m for m in sys.modules if m == 'veloce' or m.startswith('veloce.')]))\n"
        "print('pydantic' in sys.modules)"
    )
    loaded, pydantic = out.splitlines()
    assert int(loaded) <= _BARE_IMPORT_MODULE_BUDGET
    assert pydantic == "False"


def test_a_name_loads_its_module_on_first_access_and_is_cached():
    out = _run(
        "import sys, veloce\n"
        "before = 'veloce.app.core' in sys.modules\n"
        "cls = veloce.Veloce\n"
        "print(before, 'veloce.app.core' in sys.modules, 'Veloce' in vars(veloce))"
    )
    assert out == "False True True"


@pytest.mark.filterwarnings("ignore::veloce.VeloceDeprecationWarning")
@pytest.mark.parametrize("name", veloce.__all__)
def test_every_public_name_resolves(name):
    assert getattr(veloce, name) is not None


def test_dir_lists_the_public_names():
    assert set(veloce.__all__) <= set(dir(veloce))


def test_a_submodule_is_reachable_as_an_attribute():
    out = _run("import veloce\nprint(veloce.http.__name__)")
    assert out == "veloce.http"


def test_an_unknown_name_raises_attribute_error():
    with pytest.raises(AttributeError, match="no attribute 'NoSuchName'"):
        getattr(veloce, "NoSuchName")


def _modules() -> list[str]:
    root = SRC / "veloce"
    found = []
    for path in sorted(root.rglob("*.py")):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts != ["veloce"]:
            found.append(".".join(parts))
    return found


def test_every_module_imports_first_in_a_fresh_interpreter():
    def attempt(module: str) -> tuple[str, str]:
        done = subprocess.run(
            [sys.executable, "-I", "-c", f"import {module}"], capture_output=True, text=True
        )
        return module, done.stderr.strip().splitlines()[-1] if done.returncode else ""

    with ThreadPoolExecutor(8) as pool:
        failures = [(m, e) for m, e in pool.map(attempt, _modules()) if e]
    assert failures == []
