"""@app.shell_context_processor + make_shell_context."""

from __future__ import annotations

import sys
import textwrap

from veloce import Veloce
from veloce.cli import build_parser, main


def test_default_context_has_app_and_g():
    app = Veloce(openapi_url=None)
    ctx = app.make_shell_context()
    assert ctx["app"] is app
    assert "g" in ctx


def test_processor_dict_merges_into_context():
    app = Veloce(openapi_url=None)

    @app.shell_context_processor
    def add_models():
        return {"User": "USER_MODEL", "Post": "POST_MODEL"}

    ctx = app.make_shell_context()
    assert ctx["User"] == "USER_MODEL"
    assert ctx["Post"] == "POST_MODEL"
    # Defaults still present.
    assert ctx["app"] is app


def test_later_processor_wins_on_conflict():
    app = Veloce(openapi_url=None)

    @app.shell_context_processor
    def a():
        return {"x": 1}

    @app.shell_context_processor
    def b():
        return {"x": 2}

    assert app.make_shell_context()["x"] == 2


def test_processor_returning_none_or_empty_is_safe():
    app = Veloce(openapi_url=None)

    @app.shell_context_processor
    def empty():
        return None

    @app.shell_context_processor
    def actual():
        return {"y": 3}

    ctx = app.make_shell_context()
    assert ctx["y"] == 3


def test_cli_shell_subcommand_registered():

    parser = build_parser()
    args = parser.parse_args(["shell", "demo:app"])
    assert args.command == "shell"
    assert args.app == "demo:app"


def test_cli_shell_invokes_interact(tmp_path, monkeypatch):
    """`veloce shell` builds the context and calls code.interact."""
    module_path = tmp_path / "cli_shell_app.py"
    module_path.write_text(
        textwrap.dedent(
            """
            from veloce import Veloce
            app = Veloce(openapi_url=None)

            @app.shell_context_processor
            def stuff():
                return {"X": 42}
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cli_shell_app", None)

    captured: dict = {}

    def fake_interact(banner: str, local: dict) -> None:
        captured["banner"] = banner
        captured["local"] = local

    monkeypatch.setattr("code.interact", fake_interact)

    rc = main(["shell", "cli_shell_app:app"])
    assert rc == 0
    assert captured["local"]["X"] == 42
    assert "app" in captured["local"]
    assert "Veloce shell" in captured["banner"]
