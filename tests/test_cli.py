"""veloce CLI (C1-C2)."""

from __future__ import annotations

import sys
import textwrap

import pytest

from veloce import __version__
from veloce.cli import _apply_env_file, _load_app, build_parser, main


def test_parser_has_run_and_routes():
    parser = build_parser()
    # `run` subcommand
    args = parser.parse_args(["run", "demo:app"])
    assert args.command == "run"
    assert args.app == "demo:app"
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.reload is False
    assert args.workers == 1

    # `routes` subcommand
    args = parser.parse_args(["routes", "demo:app"])
    assert args.command == "routes"
    assert args.app == "demo:app"


def test_version_flag_prints_and_exits(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    output = (captured.out + captured.err).strip()
    assert output == f"veloce {__version__}"


def test_run_accepts_overrides():
    parser = build_parser()
    args = parser.parse_args(
        ["run", "x:y", "--host", "0.0.0.0", "--port", "9000", "--reload", "--workers", "4"]
    )
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.reload is True
    assert args.workers == 4


def test_load_app_bad_form_raises():
    with pytest.raises(SystemExit, match="module:attribute"):
        _load_app("no-colon-here")


def test_load_app_unknown_module_raises():
    with pytest.raises(SystemExit, match="Could not import"):
        _load_app("definitely_not_a_module_zzz:app")


def test_load_app_missing_attribute_raises(tmp_path, monkeypatch):
    module_path = tmp_path / "dummy_cli_app.py"
    module_path.write_text("x = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(SystemExit, match="has no attribute 'app'"):
        _load_app("dummy_cli_app:app")


def test_load_app_returns_attribute(tmp_path, monkeypatch):
    module_path = tmp_path / "cli_demo_app.py"
    module_path.write_text(
        textwrap.dedent(
            """
            from veloce import Veloce
            app = Veloce(openapi_url=None)
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    # Module may already be cached from a prior test — drop it.
    sys.modules.pop("cli_demo_app", None)
    obj = _load_app("cli_demo_app:app")
    assert obj.title == "Veloce"


def test_routes_command_prints_table(tmp_path, monkeypatch, capsys):
    module_path = tmp_path / "cli_routes_app.py"
    module_path.write_text(
        textwrap.dedent(
            """
            from veloce import Veloce
            app = Veloce(openapi_url=None)

            @app.get("/hello", name="hello")
            async def hello():
                return {}

            @app.post("/items/{id:int}", name="create_item")
            async def create(id: int):
                return {}
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cli_routes_app", None)
    rc = main(["routes", "cli_routes_app:app"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "METHOD" in out and "PATH" in out and "NAME" in out
    assert "GET" in out and "/hello" in out and "hello" in out
    assert "POST" in out and "/items/{id}" in out and "create_item" in out


def test_routes_command_no_routes(tmp_path, monkeypatch, capsys):
    module_path = tmp_path / "cli_empty_app.py"
    module_path.write_text("from veloce import Veloce\napp = Veloce(openapi_url=None)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cli_empty_app", None)
    rc = main(["routes", "cli_empty_app:app"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No routes registered" in out


# ── S7: `veloce check` security audit command ─────────────────────────


def test_check_command_reports_issues(tmp_path, monkeypatch, capsys):
    module_path = tmp_path / "cli_check_bad.py"
    module_path.write_text(
        textwrap.dedent(
            """
            from veloce import Veloce
            app = Veloce(debug=True, openapi_url=None)
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cli_check_bad", None)
    rc = main(["check", "cli_check_bad:app"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "issue" in out.lower()


def test_check_command_clean_app(tmp_path, monkeypatch, capsys):
    module_path = tmp_path / "cli_check_good.py"
    module_path.write_text(
        textwrap.dedent(
            """
            from veloce import Veloce
            app = Veloce(openapi_url=None)
            app.config["SECRET_KEY"] = "a-real-secret"
            app.use_secure_defaults()
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cli_check_good", None)
    rc = main(["check", "cli_check_good:app"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no issues" in out.lower()


# ── --env-file dotenv auto-load ───────────────────────────────────────


def test_run_parser_has_env_file_flags():
    parser = build_parser()
    args = parser.parse_args(["run", "demo:app", "--env-file", ".env.test"])
    assert args.env_file == ".env.test"
    assert args.no_env_file is False

    args = parser.parse_args(["run", "demo:app", "--no-env-file"])
    assert args.no_env_file is True
    assert args.env_file is None


def test_env_file_populates_environ(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("CLI_ENV_KEY=from_file\nexport CLI_ENV_OTHER='quoted value'\n")
    monkeypatch.delenv("CLI_ENV_KEY", raising=False)
    monkeypatch.delenv("CLI_ENV_OTHER", raising=False)

    parser = build_parser()
    args = parser.parse_args(["run", "demo:app", "--env-file", str(env)])
    _apply_env_file(args)

    import os

    assert os.environ["CLI_ENV_KEY"] == "from_file"
    assert os.environ["CLI_ENV_OTHER"] == "quoted value"


def test_no_env_file_disables_loading(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("CLI_ENV_DISABLED=should_not_load\n")
    monkeypatch.delenv("CLI_ENV_DISABLED", raising=False)

    parser = build_parser()
    args = parser.parse_args(["run", "demo:app", "--env-file", str(env), "--no-env-file"])
    _apply_env_file(args)

    import os

    assert "CLI_ENV_DISABLED" not in os.environ


def test_env_file_does_not_overwrite_existing_environ(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("CLI_ENV_PRESET=from_file\n")
    monkeypatch.setenv("CLI_ENV_PRESET", "already_set")

    parser = build_parser()
    args = parser.parse_args(["run", "demo:app", "--env-file", str(env)])
    _apply_env_file(args)

    import os

    # Real environ wins — the file value is ignored.
    assert os.environ["CLI_ENV_PRESET"] == "already_set"


def test_explicit_missing_env_file_errors(tmp_path):
    missing = tmp_path / "nope.env"
    parser = build_parser()
    args = parser.parse_args(["run", "demo:app", "--env-file", str(missing)])
    with pytest.raises(SystemExit, match="Could not read env file"):
        _apply_env_file(args)


def test_auto_discover_missing_default_is_silent(tmp_path, monkeypatch):
    # CWD with no .env — auto-discovery must not raise.
    monkeypatch.chdir(tmp_path)
    parser = build_parser()
    args = parser.parse_args(["run", "demo:app"])
    _apply_env_file(args)  # no exception
