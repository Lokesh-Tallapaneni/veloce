"""app.cli Click group + `veloce custom` subcommand."""

from __future__ import annotations

import sys
import textwrap

import pytest

from veloce import Veloce


def test_app_cli_is_click_group():
    click = pytest.importorskip("click")
    app = Veloce(title="Demo", openapi_url=None)
    assert isinstance(app.cli, click.Group)


def test_app_cli_lazy_singleton():
    """Accessing `.cli` twice returns the same group object."""
    pytest.importorskip("click")
    app = Veloce(openapi_url=None)
    g1 = app.cli
    g2 = app.cli
    assert g1 is g2


def test_app_cli_accepts_commands():
    pytest.importorskip("click")
    app = Veloce(openapi_url=None)

    captured: list[str] = []

    @app.cli.command("ping")
    def ping():
        captured.append("pong")

    app.cli.main(["ping"], standalone_mode=False)
    assert captured == ["pong"]


def test_cli_custom_subcommand_runs_app_command(tmp_path, monkeypatch, capsys):
    pytest.importorskip("click")
    module_path = tmp_path / "cli_custom_app.py"
    module_path.write_text(
        textwrap.dedent(
            """
            from veloce import Veloce
            app = Veloce(openapi_url=None)

            @app.cli.command("greet")
            def greet():
                print("hi from custom")
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cli_custom_app", None)

    from veloce.cli import main

    rc = main(["custom", "cli_custom_app:app", "greet"])
    assert rc == 0
    assert "hi from custom" in capsys.readouterr().out


def test_cli_custom_help_path(tmp_path, monkeypatch, capsys):
    pytest.importorskip("click")
    module_path = tmp_path / "cli_help_app.py"
    module_path.write_text(
        textwrap.dedent(
            """
            from veloce import Veloce
            app = Veloce(title="HelpDemo", openapi_url=None)

            @app.cli.command("aaa")
            def aaa(): pass
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cli_help_app", None)

    from veloce.cli import main

    rc = main(["custom", "cli_help_app:app", "--help"])
    out = capsys.readouterr().out
    assert "aaa" in out
    # Click exits with 0 for --help.
    assert rc == 0
