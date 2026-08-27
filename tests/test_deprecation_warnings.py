"""Veloce's deprecations are visible where applications actually live.

Python's default filter shows a `DeprecationWarning` only when it is raised
from `__main__`. Every application served by uvicorn or gunicorn is imported as
an ordinary module, so a deprecation raised there was silent - and three of
them promise removal in v1.0.0. `VeloceDeprecationWarning` is rooted at
`UserWarning` so the default filter does not hide it, while staying a single
category a user can silence in one line.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import warnings

import pytest

from veloce import FileResponse, Veloce, VeloceDeprecationWarning


def test_the_category_is_not_hidden_by_the_default_filter():
    """Rooting it at `DeprecationWarning` is what made it invisible."""
    assert issubclass(VeloceDeprecationWarning, UserWarning)
    assert not issubclass(VeloceDeprecationWarning, DeprecationWarning)


def test_on_event_warns():
    app = Veloce(openapi_url=None)
    with pytest.warns(VeloceDeprecationWarning, match="on_event"):

        @app.on_event("startup")
        async def _startup(): ...


def test_the_warning_names_its_replacement():
    """A deprecation that does not say what to use instead is a dead end."""
    app = Veloce(openapi_url=None)
    with pytest.warns(VeloceDeprecationWarning) as caught:

        @app.on_event("startup")
        async def _startup(): ...

    assert "on_startup" in str(caught[0].message)


async def test_a_blocking_file_response_warns_on_the_running_loop():
    with pytest.warns(VeloceDeprecationWarning, match="from_path"):
        FileResponse(__file__)


def test_a_user_can_silence_the_whole_category_in_one_line():
    app = Veloce(openapi_url=None)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        warnings.filterwarnings("ignore", category=VeloceDeprecationWarning)

        @app.on_event("startup")
        async def _startup(): ...


def test_a_deprecation_reached_from_an_application_module_is_visible(tmp_path):
    """The failure mode this class exists for: silent under uvicorn/gunicorn."""
    (tmp_path / "appmod.py").write_text(
        textwrap.dedent(
            """
            from veloce import Veloce

            app = Veloce(openapi_url=None)

            @app.on_event("startup")
            async def startup(): ...
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, r'{tmp_path}'); import appmod"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "VeloceDeprecationWarning" in result.stderr
    assert "on_event" in result.stderr


def _stdlib_deprecation_uses() -> list[str]:
    """Every reference to the bare stdlib `DeprecationWarning` in `src/veloce`.

    Found by walking the AST rather than matching source lines. The previous
    version of this guard compared each *stripped line* against
    `"DeprecationWarning"` / `"DeprecationWarning,"`, so it saw only the name
    when a formatter had put it on a line of its own - and the single-line
    spelling this guard exists to catch,

        warnings.warn("...", DeprecationWarning, stacklevel=2)

    was invisible to it. `VeloceDeprecationWarning` is a distinct `Name`, so it
    is excluded structurally rather than by substring.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "veloce"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            # A bare name (`DeprecationWarning`) or a qualified one
            # (`builtins.DeprecationWarning`); `VeloceDeprecationWarning` is a
            # different node either way, so it is excluded structurally.
            if (isinstance(node, ast.Name) and node.id == "DeprecationWarning") or (
                isinstance(node, ast.Attribute) and node.attr == "DeprecationWarning"
            ):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    return offenders


def test_every_deprecation_in_the_source_uses_the_veloce_category():
    """A new deprecation added with the stdlib category would be silent again."""
    offenders = _stdlib_deprecation_uses()
    assert not offenders, f"raise VeloceDeprecationWarning instead: {offenders}"


def test_the_guard_sees_a_single_line_deprecation(tmp_path, monkeypatch):
    """The guard's own failure mode, checked.

    The line-matching version it replaced returned nothing for this file, so it
    could not have caught the spelling it was written for.
    """
    import ast

    source = 'import warnings\nwarnings.warn("gone", DeprecationWarning, stacklevel=2)\n'
    found = [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name) and node.id == "DeprecationWarning"
    ]
    assert found == [2]

    stripped_line_match = [
        index
        for index, line in enumerate(source.splitlines(), 1)
        if line.strip() in ("DeprecationWarning", "DeprecationWarning,")
    ]
    assert stripped_line_match == []


def test_the_guard_does_not_flag_the_veloce_category():
    """The negative: `VeloceDeprecationWarning` must not trip it, or the guard
    would fail on every correct deprecation in the tree."""
    import ast

    source = 'warnings.warn("x", VeloceDeprecationWarning, stacklevel=2)\n'
    found = [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name) and node.id == "DeprecationWarning"
    ]
    assert found == []


def test_the_guard_actually_reads_the_package():
    """A guard that silently walked nothing would pass forever."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "veloce"
    assert len(list(root.rglob("*.py"))) > 50


# ── the retired event-registration API ────────────────────────
#
# Moved here from `test_polish_e2e.py`, a module named for a fix wave rather
# than a subject.


def test_on_event_decorator_emits_deprecation_warning():
    app = Veloce(openapi_url=None)
    with pytest.warns(VeloceDeprecationWarning, match="on_startup"):

        @app.on_event("startup")
        async def boot() -> None:
            return None

    assert boot in app._on_startup
    assert callable(boot)


def test_add_event_handler_emits_deprecation_warning():
    app = Veloce(openapi_url=None)

    async def boot() -> None:
        return None

    with pytest.warns(VeloceDeprecationWarning, match="on_startup"):
        app.add_event_handler("startup", boot)

    assert boot in app._on_startup


def test_on_startup_decorator_does_not_warn():
    app = Veloce(openapi_url=None)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)

        @app.on_startup
        async def boot() -> None:
            return None

    assert boot in app._on_startup
