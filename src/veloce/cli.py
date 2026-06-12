"""Command-line interface — the `veloce` entry point.

Subcommands:

- `veloce new NAME [--template minimal|api|web]` - scaffold a new project.
- `veloce generate KIND NAME` (alias `g`) - emit a single boilerplate file
  (`route`, `blueprint`, `middleware`, `model`, `security`).
- `veloce run app:app [--host --port --reload --workers]` - serve the
  app under uvicorn when it is installed (the optional `[uvicorn]` extra),
  otherwise fall back to veloce's built-in `app.run()` server.
- `veloce routes app:app` - print the route table (method, path, name).
- `veloce check app:app` - run a pre-deploy security audit.
- `veloce shell app:app` - a REPL with the app loaded.
- `veloce custom app:app -- ...` - run an `app.cli` (Click) command.

Built on `argparse` (stdlib) - keeps the dep surface small. The `new` and
`generate` scaffolders import `veloce._scaffold` lazily so the framework is
not loaded for `veloce --version` / `--help`. The
"module:attribute" reference syntax is the same one ASGI servers use,
so the same string passed to `--app` works with `uvicorn` directly.

Third-party packages can add their own subcommands by advertising a
`veloce.commands` entry point. Discovery is lazy: a plugin is imported and
executed only when its command is the one selected on the command line, so
`veloce`, `veloce --version`, and `veloce --help` never run plugin code. See
`_load_plugin_command`.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import importlib
import importlib.metadata
import io
import os
import sys
import warnings
from pathlib import Path
from typing import Any

from veloce._constants import MSG_APP_REFERENCE_FORM
from veloce.config import _parse_env_lines

# ── Constants ─────────────────────────────────────────────

# Default dotenv filename auto-loaded by `run`/`shell`/`custom` when the
# file exists in the CWD and `--no-env-file` was not passed.
_DEFAULT_ENV_FILE = ".env"

# Entry-point group third-party packages advertise CLI subcommands under.
# A distribution exposes a plugin via, e.g. in its pyproject.toml::
#
#     [project.entry-points."veloce.commands"]
#     deploy = "mypkg.cli:register"
#
# where `mypkg.cli:register` is a callable taking the argparse subparsers
# action and adding one parser (with a `func` default) to it.
_COMMAND_ENTRY_POINT_GROUP = "veloce.commands"


# ── Shared helpers ────────────────────────────────────────


def _resolve_version() -> str:
    # Avoid `from veloce import __version__` so `veloce --version` does not
    # drag the entire framework (router, middleware, security, sse, ...) into
    # sys.modules just to print a string. Fallback must mirror the one in
    # `veloce/__init__.py` for editable installs without resolved metadata.
    try:
        return importlib.metadata.version("veloceframework")
    except importlib.metadata.PackageNotFoundError:
        return "0.3.0"


def _load_app(reference: str) -> Any:
    """Resolve a `module:attribute` reference to the live app object.

    Adds CWD to `sys.path` first so `veloce run myapp:app` works from
    the project root without `pip install -e .`. Raises `SystemExit`
    on import or attribute errors so the CLI exits with a useful code
    instead of a Python traceback.
    """
    if ":" not in reference:
        raise SystemExit(f"App reference must be in 'module:attribute' form, got {reference!r}")
    module_name, _, attr = reference.partition(":")

    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        module = importlib.import_module(module_name)
    except ImportError as err:
        raise SystemExit(f"Could not import {module_name!r}: {err}") from err

    try:
        return getattr(module, attr)
    except AttributeError as err:
        raise SystemExit(f"Module {module_name!r} has no attribute {attr!r}") from err


def _apply_env_file(args: argparse.Namespace) -> None:
    """Populate `os.environ` from a dotenv file before the app imports.

    Subcommands that import user code (`run`, `shell`, `custom`) call
    this first so config read at import time sees the file's values.
    A real environment variable always wins - keys already present in
    `os.environ` are never overwritten. With `--no-env-file` nothing is
    loaded. An explicit `--env-file PATH` that is missing is an error;
    the auto-discovered default `.env` is loaded only when it exists.
    """
    if getattr(args, "no_env_file", False):
        return
    path = getattr(args, "env_file", None) or _DEFAULT_ENV_FILE
    explicit = getattr(args, "env_file", None) is not None
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError as err:
        if explicit:
            raise SystemExit(f"Could not read env file {path!r}: {err}") from err
        # Auto-discovery: an absent default `.env` is fine.
        return
    except OSError as err:
        # Permission denied, "is a directory", etc. are real failures even
        # for the auto-discovered default - never boot with silent loss.
        raise SystemExit(f"Could not read env file {path!r}: {err}") from err
    for key, value in _parse_env_lines(lines, source=path).items():
        os.environ.setdefault(key, value)


def _require_app_attr(app: Any, attr: str, hint: str) -> None:
    """Raise `SystemExit` with a consistent message when `app` lacks `attr`.

    Centralises the four near-identical guards in `_cmd_shell`,
    `_cmd_custom`, `_cmd_routes`, and `_cmd_check` so a future rename of
    `make_shell_context` / `cli` / `routes` / `security_audit` only needs
    one edit, and the error message stays uniform across subcommands.
    """
    if not hasattr(app, attr):
        raise SystemExit(f"target is not a Veloce app (missing {hint})")


# ── Subcommands ───────────────────────────────────────────


def _cmd_run(args: argparse.Namespace) -> int:
    """`veloce run` - serve the app under uvicorn, or the built-in server.

    uvicorn is an optional extra (`pip install veloceframework[uvicorn]`). When
    it is installed the app is handed to it (preserving `--reload` and
    cross-platform multi-worker); otherwise this falls back to veloce's built-in
    `app.run()` server so `veloce run` works on a plain install. `--reload` is a
    uvicorn-only feature.
    """
    _apply_env_file(args)
    app = _load_app(args.app)

    try:
        import uvicorn
    except ImportError:
        uvicorn = None  # type: ignore[assignment]

    if uvicorn is not None:
        uvicorn.run(
            args.app,
            host=args.host,
            port=args.port,
            reload=args.reload,
            workers=args.workers if args.workers > 1 else None,
            log_level=args.log_level,
        )
        return 0

    # uvicorn absent: serve with the built-in development server.
    if args.reload:
        raise SystemExit(
            "--reload requires uvicorn. Install it with: pip install veloceframework[uvicorn]"
        )
    if not callable(getattr(app, "run", None)):
        raise SystemExit(
            f"{args.app!r} has no built-in server to fall back to; install "
            "veloceframework[uvicorn] to serve it under uvicorn."
        )
    print(
        "uvicorn is not installed - serving with veloce's built-in server. "
        "Install veloceframework[uvicorn] for the recommended production server "
        "and --reload support.",
        file=sys.stderr,
    )
    # The built-in server is single-process; --workers>1 needs uvicorn or the
    # gunicorn VeloceWorker, so warn and run one process rather than passing a
    # count `run()` would reject.
    if args.workers > 1:
        print(
            f"--workers {args.workers} is ignored by the built-in server (single "
            "process); install veloceframework[uvicorn] for multiple workers.",
            file=sys.stderr,
        )
    # The native server takes `bind_all=True` rather than an all-interfaces host.
    if args.host in ("0.0.0.0", "::"):
        app.run(port=args.port, bind_all=True)
    else:
        app.run(host=args.host, port=args.port)
    return 0


def _cmd_shell(args: argparse.Namespace) -> int:
    """`veloce shell` - drop into a Python REPL with the app loaded.

    Surfaces `app` and `g` plus anything `@app.shell_context_processor`
    contributes. Uses `code.interact` so the shell is stdlib-only - no
    IPython dependency. Inside an `app_context()` so `current_app` and
    `g` resolve as if a request were active.
    """
    import code

    _apply_env_file(args)
    app = _load_app(args.app)
    _require_app_attr(app, "make_shell_context", "`.make_shell_context`")

    with app.app_context():
        ctx = app.make_shell_context()
        banner = (
            f"Veloce shell - {getattr(app, 'title', 'app')!r} loaded as `app`.\n"
            f"Locals: {', '.join(sorted(ctx))}"
        )
        code.interact(banner=banner, local=ctx)
    return 0


def _cmd_custom(args: argparse.Namespace) -> int:
    """`veloce custom app:app -- ...args...` - run an app.cli command.

    Drops into the app's Click group (built lazily from
    `@app.cli.command(...)` decorators) inside an `app.app_context()` so
    `current_app` / `g` / config resolve as if a request were active.

    Everything after `--` on the command line is forwarded verbatim to
    the Click group. With no extra args the group prints its own help.
    """
    _apply_env_file(args)
    app = _load_app(args.app)
    _require_app_attr(app, "cli", "`.cli`")
    with app.app_context():
        # `app.cli` is a `click.Group`. Call it with the remaining argv
        # and `standalone_mode=False` so we own the exit code path.
        try:
            return int(app.cli.main(args.cli_args, standalone_mode=False) or 0)
        except SystemExit as exc:  # Click raises on --help etc.
            code = exc.code
            return int(code) if isinstance(code, int) else (1 if code else 0)


def _cmd_routes(args: argparse.Namespace) -> int:
    """`veloce routes` - print the route table."""
    app = _load_app(args.app)
    _require_app_attr(app, "routes", "`.routes` property")

    rows = list(app.routes)
    if not rows:
        print("No routes registered.")
        return 0

    # Compute column widths from the data - no fixed-width truncation.
    method_w = max(len("METHOD"), max(len(r["method"]) for r in rows))
    path_w = max(len("PATH"), max(len(r["path"]) for r in rows))
    name_w = max(len("NAME"), max(len(str(r.get("name") or "")) for r in rows))

    line = f"{'METHOD':<{method_w}}  {'PATH':<{path_w}}  {'NAME':<{name_w}}"
    print(line)
    print("-" * len(line))
    for r in rows:
        print(
            f"{r['method']:<{method_w}}  {r['path']:<{path_w}}  {(r.get('name') or ''):<{name_w}}"
        )
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """`veloce check` - run a pre-deploy security audit of the app."""
    app = _load_app(args.app)
    _require_app_attr(app, "security_audit", "`.security_audit()`")

    issues = app.security_audit()
    if not issues:
        print("Security audit: no issues found.")
        return 0
    print(f"Security audit: {len(issues)} issue(s) found:")
    for issue in issues:
        print(f"  - {issue}")
    return 1


def _cmd_new(args: argparse.Namespace) -> int:
    """`veloce new` - scaffold a new project directory."""
    # Imported lazily: scaffolding is a one-shot command, so the framework load
    # it triggers is acceptable here but must not happen for `--version`/`--help`.
    from veloce._scaffold import ScaffoldError, scaffold_project

    dest_root = Path(args.dir)
    try:
        written = scaffold_project(args.name, args.template, dest_root, force=args.force)
    except ScaffoldError as err:
        raise SystemExit(str(err)) from err

    project_dir = dest_root / args.name
    print(f"Created {args.template} project: {project_dir}")
    for path in written:
        print(f"  {path.relative_to(dest_root)}")
    # The generated project is uv-native (pyproject with `[tool.uv] package=false`,
    # no requirements.txt). Print the real path (correct under `--dir`) and the
    # uv commands its README documents.
    print("\nNext steps:")
    print(f"  cd {project_dir}")
    print("  uv run veloce run app:app --reload")
    print("  uv run pytest")
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    """`veloce generate` - emit a single boilerplate file."""
    from veloce._scaffold import ScaffoldError, generate_file

    try:
        target, content = generate_file(
            args.kind, args.name, Path(args.dir), force=args.force, to_stdout=args.stdout
        )
    except ScaffoldError as err:
        raise SystemExit(str(err)) from err

    if target is None:
        sys.stdout.write(content)
        return 0
    print(f"Created {target}")
    return 0


# ── Parser construction ───────────────────────────────────


def _add_env_file_args(p: argparse.ArgumentParser) -> None:
    """Attach the shared `--env-file` / `--no-env-file` options to `p`."""
    p.add_argument(
        "--env-file",
        default=None,
        metavar="PATH",
        help="Load environment variables from this dotenv file before importing the app "
        f"(default: auto-discover {_DEFAULT_ENV_FILE!r} in the current directory).",
    )
    p.add_argument(
        "--no-env-file",
        action="store_true",
        help="Skip dotenv loading entirely.",
    )


def _split_custom_argv(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """Split a `custom` argv into the argparse head and the Click tail.

    `veloce custom [--env-file PATH | --no-env-file]... app:app [...same flags...] [--] ...args`

    The CLI's own `--env-file` / `--no-env-file` flags are parsed on either
    side of `app`, then everything else is handed to the app's Click group
    verbatim. `argparse.REMAINDER` cannot express this: it begins capturing
    at the first token after `app`, so a trailing `--env-file` would be
    swallowed into the forwarded args and the dotenv file would never load.
    We therefore peel the tail off ourselves - greedily consuming only the
    env-file flags after `app` - and let argparse parse just the head. An
    explicit `--` ends the flag region and is dropped from the forwarded
    args (POSIX convention).

    Returns `(head, tail)` where `tail is None` means "no forwarded args
    region was found" (e.g. the argv is malformed and argparse should emit
    its own usage error against the whole thing).
    """
    if not argv or argv[0] != "custom":
        return argv, None
    # Locate the `app` positional: the first non-flag token after `custom`.
    # A space-separated `--env-file PATH` placed before `app` consumes its
    # value token, so skip that value too - otherwise PATH is mistaken for
    # the app reference and the real reference is pushed into the tail.
    idx = 1
    while idx < len(argv) and argv[idx].startswith("-") and argv[idx] != "--":
        if argv[idx] == "--env-file":
            # Skip the flag and its value token.
            idx += 2
        else:
            idx += 1
    if idx >= len(argv):
        # No `app` token - let argparse report the error.
        return argv, None
    # `head` is ["custom", ..., "app"]; `rest` is the forwarded command.
    head = argv[: idx + 1]
    rest = argv[idx + 1 :]
    # Consume env-file flags that precede the forwarded command.
    cursor = 0
    while cursor < len(rest):
        token = rest[cursor]
        if token == "--no-env-file":
            head.append(token)
            cursor += 1
        elif token == "--env-file":
            head.extend(rest[cursor : cursor + 2])
            cursor += 2
        elif token.startswith("--env-file="):
            head.append(token)
            cursor += 1
        else:
            break
    tail = rest[cursor:]
    if tail and tail[0] == "--":
        tail = tail[1:]
    return head, tail


def _iter_command_entry_points() -> list[importlib.metadata.EntryPoint]:
    """Return the installed `veloce.commands` entry points (best effort).

    `EntryPoints.select(group=...)` is the stable selection API on Python
    3.10+. A broken metadata cache should not take the whole CLI down, so
    any failure to enumerate is swallowed and reported as "no plugins".
    """
    try:
        return list(importlib.metadata.entry_points().select(group=_COMMAND_ENTRY_POINT_GROUP))
    except Exception:  # pragma: no cover - corrupt distribution metadata
        return []


def _rollback_subparsers(
    sub: argparse._SubParsersAction[Any],
    *,
    keep: frozenset[str],
) -> None:
    """Remove any subparser whose name is not in `keep`.

    Undoes a partial registration: a plugin that adds one or more parsers
    and then raises (or never sets a `func`) leaves entries in the
    subparsers action's `choices` map and its help-listing actions. This
    deletes every parser added since `keep` was snapshotted so the
    documented "warn and skip" guarantee holds - a failed plugin leaves the
    parser exactly as it was before the plugin ran.
    """
    for name in [n for n in sub.choices if n not in keep]:
        del sub.choices[name]
    sub._choices_actions = [a for a in sub._choices_actions if a.dest in keep]


def _load_plugin_command(
    sub: argparse._SubParsersAction[Any],
    *,
    reserved: frozenset[str],
    name: str,
) -> None:
    """Register the single third-party subcommand named `name`, if any.

    Plugin discovery is deferred until a plugin subcommand is actually
    selected: only the entry point whose name matches `name` is loaded and
    executed, so `veloce`, `veloce --version`, `veloce --help`, and every
    built-in command run without importing or executing any plugin code.

    Each `veloce.commands` entry point loads to a callable that is handed
    the subparsers action and adds exactly one parser (with a `func`
    default) to it. Plugins are isolated from the core: a plugin that fails
    to import, does not load to a callable, raises while registering, leaves
    no `func` default, or whose name collides with a built-in is warned
    about and skipped - and any parser it partially registered is rolled
    back - so the built-in commands always remain usable.
    """
    if name in reserved:
        # A plugin may not shadow a built-in. Warn that the entry point is
        # being skipped - but do not load it: the built-in handles this name.
        for ep in _iter_command_entry_points():
            if ep.name == name:
                warnings.warn(
                    f"veloce CLI plugin {name!r} (from {ep.value!r}) collides with an existing "
                    "command; skipping.",
                    stacklevel=2,
                )
        return
    # Entry points are sorted (see `_iter_command_entry_points`), so the
    # candidates for this name are enumerated in a stable order. The first
    # one that registers a runnable command wins; any further entry point
    # sharing the name is a collision - warn and skip it deterministically
    # so behaviour never depends on install order.
    # Sort candidates by (name, value) so precedence and the collision warning
    # are deterministic regardless of entry-point iteration / install order.
    matches = sorted(
        (ep for ep in _iter_command_entry_points() if ep.name == name),
        key=lambda ep: (ep.name, ep.value),
    )
    if len(matches) > 1:
        losers = ", ".join(repr(ep.value) for ep in matches[1:])
        warnings.warn(
            f"veloce CLI plugin name {name!r} is provided by multiple entry points; using "
            f"{matches[0].value!r} and skipping: {losers}.",
            stacklevel=2,
        )
    for ep in matches[:1]:
        # `BaseException` (not just `Exception`): a plugin must not be able to
        # kill the CLI by raising `SystemExit` / `KeyboardInterrupt` from its
        # import or registration. The boundary swallows everything and skips.
        try:
            register = ep.load()
        except BaseException as err:  # noqa: BLE001 - a bad plugin must not break the CLI
            warnings.warn(
                f"veloce CLI plugin {name!r} (from {ep.value!r}) failed to load: {err!r}; "
                "skipping.",
                stacklevel=2,
            )
            continue
        if not callable(register):
            warnings.warn(
                f"veloce CLI plugin {name!r} (from {ep.value!r}) is not callable; skipping.",
                stacklevel=2,
            )
            continue
        existing = frozenset(sub.choices)
        try:
            register(sub)
        except BaseException as err:  # noqa: BLE001 - a bad plugin must not break the CLI
            _rollback_subparsers(sub, keep=existing)
            warnings.warn(
                f"veloce CLI plugin {name!r} (from {ep.value!r}) raised while registering: "
                f"{err!r}; skipping.",
                stacklevel=2,
            )
            continue
        added = sub.choices.get(name)
        registered_func = getattr(added, "_defaults", {}).get("func") if added else None
        if added is None or not callable(registered_func):
            # A well-behaved plugin adds a parser named `name` with a CALLABLE
            # `func` default. Without it, `main()` would crash; roll the
            # registration back and skip instead (a non-callable `func=123`
            # is treated the same as a missing one).
            _rollback_subparsers(sub, keep=existing)
            warnings.warn(
                f"veloce CLI plugin {name!r} (from {ep.value!r}) did not register a runnable "
                f"{name!r} command; skipping.",
                stacklevel=2,
            )
            continue
        return


class _VeloceArgumentParser(argparse.ArgumentParser):
    """Top-level parser that special-cases `veloce custom` argv.

    See `_split_custom_argv` for why the forwarded Click args cannot use
    `argparse.REMAINDER` without breaking `--env-file` placed before them.
    """

    def parse_known_args(  # type: ignore[override]
        self,
        args: list[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> tuple[argparse.Namespace, list[str]]:
        argv = list(sys.argv[1:] if args is None else args)
        head, tail = _split_custom_argv(argv)
        parsed, extras = super().parse_known_args(head, namespace)
        if tail is not None:
            parsed.cli_args = tail
        return parsed, extras


def build_parser(plugin_command: str | None = None) -> argparse.ArgumentParser:
    """Build the top-level argparse parser. Exposed for testing.

    Built-in commands are always registered. Plugin discovery is deferred:
    only when `plugin_command` names a command that is not a built-in is the
    matching `veloce.commands` entry point loaded and executed. With no
    `plugin_command` (the default) no plugin code runs at all, so building
    the parser for `--version` / `--help` never triggers plugin imports.
    """
    parser = _VeloceArgumentParser(
        prog="veloce",
        description="Veloce - ultra-fast async Python web framework.",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"veloce {_resolve_version()}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="Scaffold a new Veloce project.")
    p_new.add_argument("name", help="Project name (also the directory created).")
    p_new.add_argument(
        "--template",
        "-t",
        default="minimal",
        metavar="NAME",
        help="Project template: minimal, api, or web (default: minimal).",
    )
    p_new.add_argument(
        "--dir",
        default=".",
        metavar="PATH",
        help="Parent directory to create the project in (default: current directory).",
    )
    p_new.add_argument(
        "--force",
        action="store_true",
        help="Write into a target directory that already exists and is not empty.",
    )
    p_new.set_defaults(func=_cmd_new)

    p_gen = sub.add_parser(
        "generate",
        aliases=["g"],
        help="Generate a single boilerplate file.",
    )
    p_gen.add_argument(
        "kind",
        metavar="KIND",
        help="What to generate: route, blueprint, middleware, model, or security.",
    )
    p_gen.add_argument("name", help="Name for the generated symbol and file.")
    p_gen.add_argument(
        "--dir",
        default=".",
        metavar="PATH",
        help="Directory to write the file into (default: current directory).",
    )
    p_gen.add_argument(
        "--stdout",
        action="store_true",
        help="Print the generated file to stdout instead of writing it.",
    )
    p_gen.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing file.",
    )
    p_gen.set_defaults(func=_cmd_generate)

    p_run = sub.add_parser(
        "run", help="Run the app under uvicorn (or the built-in server if uvicorn is absent)."
    )
    p_run.add_argument("app", help=MSG_APP_REFERENCE_FORM)
    p_run.add_argument("--host", default="127.0.0.1")
    p_run.add_argument("--port", type=int, default=8000)
    p_run.add_argument("--reload", action="store_true", help="Auto-reload on code changes.")
    p_run.add_argument("--workers", type=int, default=1)
    p_run.add_argument("--log-level", default="info")
    _add_env_file_args(p_run)
    p_run.set_defaults(func=_cmd_run)

    p_routes = sub.add_parser("routes", help="Print the route table.")
    p_routes.add_argument("app", help=MSG_APP_REFERENCE_FORM)
    p_routes.set_defaults(func=_cmd_routes)

    p_check = sub.add_parser("check", help="Run a pre-deploy security audit.")
    p_check.add_argument("app", help=MSG_APP_REFERENCE_FORM)
    p_check.set_defaults(func=_cmd_check)

    p_shell = sub.add_parser("shell", help="Interactive Python shell with the app loaded.")
    p_shell.add_argument("app", help=MSG_APP_REFERENCE_FORM)
    _add_env_file_args(p_shell)
    p_shell.set_defaults(func=_cmd_shell)

    p_custom = sub.add_parser(
        "custom",
        help="Run an app.cli (Click) command defined on the app.",
    )
    p_custom.add_argument("app", help=MSG_APP_REFERENCE_FORM)
    _add_env_file_args(p_custom)
    # The forwarded Click argv is peeled off after a literal `--` by
    # `_VeloceArgumentParser.parse_known_args`; argparse only ever sees the
    # head. Default to no extra args when `--` is absent.
    p_custom.set_defaults(func=_cmd_custom, cli_args=[])

    # Built-in names are reserved; a plugin may not shadow them. `sub.choices`
    # holds every subparser registered above, so it stays correct as commands
    # are added or removed without a hand-maintained list. Only the selected
    # plugin command (if any) is loaded - never the whole entry-point group.
    if plugin_command is not None:
        _load_plugin_command(sub, reserved=frozenset(sub.choices), name=plugin_command)

    return parser


# ── Entry point ───────────────────────────────────────────


def _candidate_plugin_command(argv: list[str] | None) -> str | None:
    """Return the chosen subcommand name iff it is an UNKNOWN (plugin) command.

    Plugin discovery must never run before argv is validated. A PLUGIN-FREE
    parser vets the whole argv first:

    - parse succeeds -> a built-in was selected; no plugin needed -> `None`.
    - argparse exits for `-h`/`--help`/`-V`/`--version` (exit 0), a bad
      option, or a bad value -> those must not load plugins -> `None` (the real
      parse in `main` reproduces the identical exit/usage).
    - argparse exits with a usage error (code 2) AND the first positional
      token is an unknown command name -> that name is a plugin candidate.

    The first-positional check runs only inside the code-2 branch, after
    argparse has already rejected the argv, so a malformed argv never reaches
    discovery; and the name is confirmed to be a real positional (every token
    before it is a recognised global option), never an option's value.

    This probe is SILENT: argparse prints usage/help/version before raising
    `SystemExit`, and the real parse in `main` prints the canonical message -
    so the probe's own output is suppressed to avoid emitting it twice.
    """
    strict = build_parser(plugin_command=None)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            strict.parse_args(argv)
    except SystemExit as exit_err:
        if exit_err.code != 2:
            # Help / version exits: not an error, not a plugin.
            return None
        tokens = sys.argv[1:] if argv is None else list(argv)
        candidate = _first_positional(tokens)
        if candidate is not None and candidate not in _builtin_command_names():
            return candidate
        return None
    return None


def _first_positional(tokens: list[str]) -> str | None:
    """First bare token, provided every preceding token is a global option.

    Returns `None` if a non-global option appears first (so an unknown global
    flag like `--bogus` is never mistaken for - nor allowed to mask - a
    command name).
    """
    for token in tokens:
        if token in ("-h", "--help", "-V", "--version"):
            # argparse would have acted on these already.
            return None
        if token.startswith("-"):
            # An unknown option token: not a clean command selection.
            return None
        return token
    return None


@functools.cache
def _builtin_command_names() -> frozenset[str]:
    """The built-in subcommand names, read once from the plugin-free parser.

    Cached because the built-in command set is fixed for the process - the
    plugin-free parser always has the same shape, so there is nothing to
    stale-cache. Reads `parser._actions` / `argparse._SubParsersAction`, which
    are private argparse internals: stable across CPython 3.x but not part of
    the public API. `tests/test_cli.py::test_builtin_command_names_introspection`
    guards this so a future stdlib refactor fails loudly rather than silently
    returning an empty set (which would misclassify every built-in as a plugin).
    """
    parser = build_parser(plugin_command=None)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return frozenset(action.choices)
    return frozenset()


def main(argv: list[str] | None = None) -> int:
    """Entry point for the veloce CLI."""
    # First pass (plugin-free) validates argv; only a clean, unknown subcommand
    # becomes a plugin candidate. `-h`, `--version`, and invalid argv never
    # load a plugin - they fall straight through to the real parse below.
    plugin_command = _candidate_plugin_command(argv)
    parser = build_parser(plugin_command=plugin_command)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
