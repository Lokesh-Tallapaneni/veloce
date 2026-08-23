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


def test_every_deprecation_in_the_source_uses_the_veloce_category():
    """A new deprecation added with the stdlib category would be silent again."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "veloce"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for index, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped == "DeprecationWarning," or stripped == "DeprecationWarning":
                offenders.append(f"{path.relative_to(root)}:{index}")
    assert not offenders, f"raise VeloceDeprecationWarning instead: {offenders}"
