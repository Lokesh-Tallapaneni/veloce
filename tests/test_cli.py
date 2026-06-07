"""veloce CLI (C1-C2)."""

from __future__ import annotations

import sys
import textwrap

import pytest

from veloce import __version__
from veloce import cli as cli_module
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


def test_run_falls_back_to_native_when_uvicorn_absent(monkeypatch):
    # uvicorn is an optional extra; with it unimportable, `veloce run` serves
    # through the built-in `app.run()` server.
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    calls: dict = {}

    class _FakeApp:
        def run(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(cli_module, "_load_app", lambda ref: _FakeApp())
    args = build_parser().parse_args(["run", "x:y", "--port", "9001"])
    assert cli_module._cmd_run(args) == 0
    assert calls == {"host": "127.0.0.1", "port": 9001}


def test_run_native_ignores_multiple_workers(monkeypatch, capsys):
    # The built-in server is single-process; --workers>1 is warned and dropped
    # (not forwarded to run(), which would reject it).
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    calls: dict = {}

    class _FakeApp:
        def run(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(cli_module, "_load_app", lambda ref: _FakeApp())
    args = build_parser().parse_args(["run", "x:y", "--port", "9001", "--workers", "4"])
    assert cli_module._cmd_run(args) == 0
    assert "workers" not in calls
    assert "--workers 4 is ignored" in capsys.readouterr().err


def test_run_native_maps_all_interfaces_to_bind_all(monkeypatch):
    # host 0.0.0.0 maps to the native server's bind_all=True (mutually exclusive
    # with an explicit host).
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    calls: dict = {}

    class _FakeApp:
        def run(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(cli_module, "_load_app", lambda ref: _FakeApp())
    args = build_parser().parse_args(["run", "x:y", "--host", "0.0.0.0", "--port", "9002"])
    assert cli_module._cmd_run(args) == 0
    assert calls == {"port": 9002, "bind_all": True}


def test_run_reload_without_uvicorn_errors(monkeypatch):
    # --reload is a uvicorn-only feature; without uvicorn it fails loudly.
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    monkeypatch.setattr(cli_module, "_load_app", lambda ref: object())
    args = build_parser().parse_args(["run", "x:y", "--reload"])
    with pytest.raises(SystemExit, match="--reload requires uvicorn"):
        cli_module._cmd_run(args)


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


def test_auto_discover_unreadable_default_errors(tmp_path, monkeypatch):
    # An auto-discovered `.env` that exists but cannot be read (here: a
    # directory in its place) is a real failure, not a silent skip — booting
    # with missing config would mask broken environments.
    (tmp_path / ".env").mkdir()
    monkeypatch.chdir(tmp_path)
    parser = build_parser()
    args = parser.parse_args(["run", "demo:app"])
    with pytest.raises(SystemExit, match="Could not read env file"):
        _apply_env_file(args)


# ── `veloce custom` argv parsing (env-file before `--`) ───────────────


def test_custom_parses_env_file_before_double_dash():
    # `--env-file` placed after `app` and before `--` must be parsed as the
    # CLI flag, not swallowed into the forwarded Click argv.
    parser = build_parser()
    args = parser.parse_args(["custom", "demo:app", "--env-file", "x.env", "--", "hello"])
    assert args.command == "custom"
    assert args.app == "demo:app"
    assert args.env_file == "x.env"
    assert args.no_env_file is False
    assert args.cli_args == ["hello"]


def test_custom_parses_no_env_file_before_double_dash():
    parser = build_parser()
    args = parser.parse_args(
        ["custom", "demo:app", "--no-env-file", "--", "cmd", "--flag", "value"]
    )
    assert args.no_env_file is True
    assert args.env_file is None
    # Dashes after `--` are forwarded verbatim, not interpreted by argparse.
    assert args.cli_args == ["cmd", "--flag", "value"]


def test_custom_without_double_dash_has_empty_cli_args():
    parser = build_parser()
    args = parser.parse_args(["custom", "demo:app", "--env-file", "y.env"])
    assert args.env_file == "y.env"
    assert args.cli_args == []


def test_custom_forwards_args_when_no_env_flags():
    parser = build_parser()
    args = parser.parse_args(["custom", "demo:app", "--", "hello", "world"])
    assert args.env_file is None
    assert args.cli_args == ["hello", "world"]


def test_custom_parses_space_env_file_before_app():
    # `--env-file PATH` (space-separated) placed before the app reference must
    # consume its value rather than mistaking PATH for the app — the documented
    # `--env-file PATH` form has to work in either position, like the `=` form.
    parser = build_parser()
    args = parser.parse_args(["custom", "--env-file", "x.env", "demo:app", "--", "hello"])
    assert args.app == "demo:app"
    assert args.env_file == "x.env"
    assert args.cli_args == ["hello"]


def test_custom_equals_env_file_before_app():
    parser = build_parser()
    args = parser.parse_args(["custom", "--env-file=x.env", "demo:app", "--", "hello"])
    assert args.app == "demo:app"
    assert args.env_file == "x.env"
    assert args.cli_args == ["hello"]


def test_custom_no_env_file_before_app():
    parser = build_parser()
    args = parser.parse_args(["custom", "--no-env-file", "demo:app", "--", "run"])
    assert args.app == "demo:app"
    assert args.no_env_file is True
    assert args.cli_args == ["run"]


# ── Entry-point plugin command discovery ──────────────────────────────


class _FakeEntryPoint:
    """Minimal stand-in for `importlib.metadata.EntryPoint`.

    Only `name`, `value`, and `load()` are exercised by the discovery code.
    """

    def __init__(self, name, value, target):
        self.name = name
        self.value = value
        self._target = target

    def load(self):
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def _patch_entry_points(monkeypatch, entry_points):
    """Make `_iter_command_entry_points` return `entry_points`."""
    monkeypatch.setattr(cli_module, "_iter_command_entry_points", lambda: list(entry_points))


def test_plugin_command_is_registered(monkeypatch):
    def register(sub):
        p = sub.add_parser("deploy", help="Deploy the app.")
        p.add_argument("target")
        p.set_defaults(func=lambda args: 0)

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("deploy", "mypkg.cli:register", register)])
    # Discovery is deferred: the parser only sees the plugin when its command
    # is the selected one.
    parser = build_parser("deploy")
    args = parser.parse_args(["deploy", "prod"])
    assert args.command == "deploy"
    assert args.target == "prod"
    assert args.func(args) == 0


def test_plugin_not_loaded_without_plugin_command(monkeypatch):
    # Without a selected plugin command, no entry point is loaded or executed
    # — building the parser for `--version` / `--help` must not run plugin code.
    loaded = {"count": 0}

    def register(sub):  # pragma: no cover — must never run here
        loaded["count"] += 1
        sub.add_parser("deploy").set_defaults(func=lambda args: 0)

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("deploy", "mypkg.cli:register", register)])
    parser = build_parser()
    assert "deploy" not in parser._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    assert loaded["count"] == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["-h", "deploy"],
        ["--help", "deploy"],
        ["-V", "deploy"],
        ["--version", "deploy"],
    ],
)
def test_global_flag_before_plugin_name_does_not_load_plugin(argv, monkeypatch):
    # `veloce -h deploy` / `veloce --version deploy`: argparse acts on the
    # global flag and exits 0 before any subcommand. Plugin discovery must not
    # run, so the entry point's loader is never called even though `deploy`
    # appears in argv.
    loaded = {"count": 0}

    def register(sub):  # pragma: no cover — must never run here
        loaded["count"] += 1
        sub.add_parser("deploy").set_defaults(func=lambda args: 0)

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("deploy", "mypkg.cli:register", register)])
    # The global flag makes `main` exit 0 (help/version), never loading deploy.
    with pytest.raises(SystemExit) as exc:
        cli_module.main(argv)
    assert exc.value.code == 0
    assert loaded["count"] == 0


def test_invalid_global_option_does_not_load_plugin(monkeypatch):
    # `veloce --bogus deploy`: the unknown option is an argparse usage error;
    # plugin discovery must NOT run before argv is rejected.
    loaded = {"count": 0}

    def register(sub):  # pragma: no cover — must never run here
        loaded["count"] += 1
        sub.add_parser("deploy").set_defaults(func=lambda args: 0)

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("deploy", "mypkg.cli:register", register)])
    with pytest.raises(SystemExit) as exc:
        cli_module.main(["--bogus", "deploy"])
    assert exc.value.code == 2  # argparse usage error, not a plugin dispatch
    assert loaded["count"] == 0


@pytest.mark.parametrize(
    "argv, expect_in",
    [
        (["--version"], "veloce "),
        (["--bogus", "deploy"], "usage:"),
    ],
)
def test_argv_validation_output_is_not_duplicated(argv, expect_in, capsys):
    # The plugin-free probe pass must be silent: argparse prints usage/help/
    # version before exiting, and the real parse prints the canonical message,
    # so the message must appear exactly once — not doubled by the two passes.
    with pytest.raises(SystemExit):
        cli_module.main(argv)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert combined.count(expect_in) == 1, combined


def test_plugin_entry_point_not_loaded_for_builtin_command(monkeypatch):
    # Selecting a built-in command must not load or execute any plugin entry
    # point, even one whose name does not collide with a built-in.
    class _NeverLoad(_FakeEntryPoint):
        def load(self):  # pragma: no cover — must never be called
            raise AssertionError("entry point loaded for a built-in command")

    _patch_entry_points(monkeypatch, [_NeverLoad("deploy", "mypkg.cli:register", None)])
    parser = build_parser("run")
    args = parser.parse_args(["run", "demo:app"])
    assert args.command == "run"


def test_plugin_command_dispatches_through_main(monkeypatch):
    calls = {}

    def register(sub):
        p = sub.add_parser("greet")
        p.add_argument("who")
        p.set_defaults(func=lambda args: calls.setdefault("who", args.who) and 0 or 7)

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("greet", "mypkg.cli:register", register)])
    rc = main(["greet", "world"])
    assert rc == 7
    assert calls["who"] == "world"


def test_plugin_name_collision_with_builtin_is_skipped(monkeypatch):
    def register(sub):
        sub.add_parser("run")  # would clash with the built-in `run`

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("run", "evil.cli:register", register)])
    with pytest.warns(UserWarning, match="collides with an existing command"):
        parser = build_parser("run")
    # The built-in `run` survives intact — its own options are still present.
    args = parser.parse_args(["run", "demo:app", "--port", "9001"])
    assert args.command == "run"
    assert args.port == 9001


def test_plugin_load_failure_is_warned_and_skipped(monkeypatch):
    _patch_entry_points(
        monkeypatch,
        [_FakeEntryPoint("broken", "missing.mod:reg", ImportError("no module named missing"))],
    )
    with pytest.warns(UserWarning, match="failed to load"):
        parser = build_parser("broken")
    # Built-ins remain usable despite the broken plugin.
    args = parser.parse_args(["routes", "demo:app"])
    assert args.command == "routes"


def test_plugin_non_callable_is_warned_and_skipped(monkeypatch):
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("notfunc", "mypkg.cli:thing", object())])
    with pytest.warns(UserWarning, match="is not callable"):
        parser = build_parser("notfunc")
    args = parser.parse_args(["check", "demo:app"])
    assert args.command == "check"


def test_plugin_registration_error_is_warned_and_skipped(monkeypatch):
    def register(sub):
        raise RuntimeError("boom")

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("bad", "mypkg.cli:register", register)])
    with pytest.warns(UserWarning, match="raised while registering"):
        parser = build_parser("bad")
    args = parser.parse_args(["shell", "demo:app"])
    assert args.command == "shell"


def test_plugin_non_callable_func_is_warned_and_skipped(monkeypatch):
    # A plugin that registers a parser whose `func` default is not callable
    # (e.g. `set_defaults(func=123)`) must be rolled back and skipped, not
    # accepted and later crashed in main().
    def register(sub):
        sub.add_parser("noncallable").set_defaults(func=123)

    _patch_entry_points(
        monkeypatch, [_FakeEntryPoint("noncallable", "mypkg.cli:register", register)]
    )
    with pytest.warns(UserWarning, match="did not register a runnable"):
        parser = build_parser("noncallable")
    choices = parser._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    assert "noncallable" not in choices


def test_plugin_raising_systemexit_does_not_kill_the_cli(monkeypatch):
    # A plugin that calls sys.exit() / raises SystemExit during load or
    # registration must be isolated and skipped — never abort the whole CLI.
    def register(sub):  # pragma: no cover — raises before adding anything
        raise SystemExit("plugin kill switch")

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("evil", "mypkg.cli:register", register)])
    with pytest.warns(UserWarning, match="raised while registering"):
        parser = build_parser("evil")
    choices = parser._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    assert "evil" not in choices


def test_plugin_partial_registration_is_rolled_back(monkeypatch):
    # A plugin that adds its parser and then raises must leave the parser in
    # the exact pre-registration state — no half-registered `half` command.
    def register(sub):
        p = sub.add_parser("half", help="Half-built.")
        p.add_argument("x")
        raise RuntimeError("exploded after add_parser")

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("half", "mypkg.cli:register", register)])
    with pytest.warns(UserWarning, match="raised while registering"):
        parser = build_parser("half")
    sub = parser._subparsers._group_actions[0]  # type: ignore[union-attr]
    assert "half" not in sub.choices
    # The leftover parser must also be gone from the help listing.
    assert all(a.dest != "half" for a in sub._choices_actions)
    # An unknown command now errors cleanly instead of dispatching a stub.
    with pytest.raises(SystemExit):
        parser.parse_args(["half", "y"])


def test_plugin_without_func_is_rolled_back_and_skipped(monkeypatch):
    # A plugin that registers a parser but never sets a `func` default would
    # crash main() with AttributeError. It must be rolled back and skipped.
    def register(sub):
        sub.add_parser("nofunc", help="No runnable handler.")

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("nofunc", "mypkg.cli:register", register)])
    with pytest.warns(UserWarning, match="did not register a runnable"):
        parser = build_parser("nofunc")
    sub = parser._subparsers._group_actions[0]  # type: ignore[union-attr]
    assert "nofunc" not in sub.choices
    assert all(a.dest != "nofunc" for a in sub._choices_actions)


def test_plugin_without_func_does_not_crash_main(monkeypatch):
    # End-to-end: a func-less plugin selected on the command line must not
    # surface as an AttributeError from main(); argparse reports the unknown
    # command instead.
    def register(sub):
        sub.add_parser("nofunc")

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("nofunc", "mypkg.cli:register", register)])
    with pytest.warns(UserWarning, match="did not register a runnable"), pytest.raises(SystemExit):
        main(["nofunc"])


def test_duplicate_plugin_name_resolves_deterministically_and_warns(monkeypatch):
    # Two entry points share the name `dup`. Resolution is deterministic — the
    # candidate with the lexicographically-smaller (name, value) wins
    # regardless of registration/iteration order — and the loser is warned
    # about and skipped, never silently dropped.
    def register_a(sub):
        sub.add_parser("dup").set_defaults(func=lambda args: "a")

    def register_b(sub):  # pragma: no cover — the lower-sorting `a` wins
        sub.add_parser("dup").set_defaults(func=lambda args: "b")

    # Register b BEFORE a to prove order-independence: a (pkg_a) still wins.
    _patch_entry_points(
        monkeypatch,
        [
            _FakeEntryPoint("dup", "pkg_b.cli:register", register_b),
            _FakeEntryPoint("dup", "pkg_a.cli:register", register_a),
        ],
    )
    with pytest.warns(UserWarning, match="multiple entry points"):
        parser = build_parser("dup")
    args = parser.parse_args(["dup"])
    assert args.func(args) == "a"


def test_no_plugins_installed_is_clean(monkeypatch):
    _patch_entry_points(monkeypatch, [])
    parser = build_parser()  # no warnings, no extra commands
    args = parser.parse_args(["run", "demo:app"])
    assert args.command == "run"


def test_iter_command_entry_points_returns_list():
    # Smoke test against the real importlib.metadata machinery — no veloce
    # plugins are installed in the test env, so an empty list is expected,
    # but the call must not raise and must return a list.
    result = cli_module._iter_command_entry_points()
    assert isinstance(result, list)


def test_builtin_command_names_introspection():
    # Guard the private-argparse-API reliance in `_builtin_command_names`
    # (`parser._actions` / `argparse._SubParsersAction`): if a future CPython
    # refactor breaks the introspection it returns an empty set, which would
    # misclassify every built-in as an unknown plugin command. Assert the real
    # built-in set instead so the breakage fails loudly here.
    names = cli_module._builtin_command_names()
    assert {"run", "routes", "check", "shell", "custom"} <= names
    # And it matches what the parser actually exposes as subcommand choices.
    parser = build_parser(plugin_command=None)
    sub_choices = set(parser._subparsers._group_actions[0].choices)  # type: ignore[union-attr]
    assert names == frozenset(sub_choices)
