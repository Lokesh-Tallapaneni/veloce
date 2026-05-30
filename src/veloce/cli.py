"""`veloce` command-line interface.

Two subcommands today:

- `veloce run app:app [--host --port --reload --workers]` — boot the
  app under uvicorn using a familiar, minimal command surface.
- `veloce routes app:app` — print the route table (method, path, name).
  Useful for sanity-checking a blueprint mount without `curl`ing each
  path.

Built on `argparse` (stdlib) — keeps the dep surface small. The
"module:attribute" reference syntax is the same one ASGI servers use,
so the same string passed to `--app` works with `uvicorn` directly.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import os
import sys
from typing import Any

from veloce.config import _parse_env_lines

# Default dotenv filename auto-loaded by `run`/`shell`/`custom` when the
# file exists in the CWD and `--no-env-file` was not passed.
_DEFAULT_ENV_FILE = ".env"


def _resolve_version() -> str:
    # Avoid `from veloce import __version__` so `veloce --version` does not
    # drag the entire framework (router, middleware, security, sse, …) into
    # sys.modules just to print a string. Fallback must mirror the one in
    # `veloce/__init__.py` for editable installs without resolved metadata.
    try:
        return importlib.metadata.version("veloceframework")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.4"


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
    A real environment variable always wins — keys already present in
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
        return  # auto-discovery: an absent default `.env` is fine
    except OSError as err:
        # Permission denied, "is a directory", etc. are real failures even
        # for the auto-discovered default — never boot with silent loss.
        raise SystemExit(f"Could not read env file {path!r}: {err}") from err
    for key, value in _parse_env_lines(lines, source=path).items():
        os.environ.setdefault(key, value)


def _cmd_run(args: argparse.Namespace) -> int:
    """`veloce run` — hand the app off to uvicorn."""
    try:
        import uvicorn
    except ImportError as err:  # pragma: no cover — only on broken envs
        raise SystemExit("uvicorn is not installed. Install it with: pip install uvicorn") from err

    _apply_env_file(args)
    _load_app(args.app)  # validate the reference before handing to uvicorn

    uvicorn.run(
        args.app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers if args.workers > 1 else None,
        log_level=args.log_level,
    )
    return 0


def _require_app_attr(app: Any, attr: str, hint: str) -> None:
    """Raise `SystemExit` with a consistent message when `app` lacks `attr`.

    Centralises the four near-identical guards in `_cmd_shell`,
    `_cmd_custom`, `_cmd_routes`, and `_cmd_check` so a future rename of
    `make_shell_context` / `cli` / `routes` / `security_audit` only needs
    one edit, and the error message stays uniform across subcommands.
    """
    if not hasattr(app, attr):
        raise SystemExit(f"target is not a Veloce app (missing {hint})")


def _cmd_shell(args: argparse.Namespace) -> int:
    """`veloce shell` — drop into a Python REPL with the app loaded.

    Surfaces `app` and `g` plus anything `@app.shell_context_processor`
    contributes. Uses `code.interact` so the shell is stdlib-only — no
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
            f"Veloce shell — {getattr(app, 'title', 'app')!r} loaded as `app`.\n"
            f"Locals: {', '.join(sorted(ctx))}"
        )
        code.interact(banner=banner, local=ctx)
    return 0


def _cmd_custom(args: argparse.Namespace) -> int:
    """`veloce custom app:app -- ...args...` — run an app.cli command.

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
    """`veloce routes` — print the route table."""
    app = _load_app(args.app)
    _require_app_attr(app, "routes", "`.routes` property")

    rows = list(app.routes)
    if not rows:
        print("No routes registered.")
        return 0

    # Compute column widths from the data — no fixed-width truncation.
    method_w = max(len(r["method"]) for r in rows + [{"method": "METHOD"}])
    path_w = max(len(r["path"]) for r in rows + [{"path": "PATH"}])
    name_w = max(len(str(r.get("name") or "")) for r in rows + [{"name": "NAME"}])

    line = f"{'METHOD':<{method_w}}  {'PATH':<{path_w}}  {'NAME':<{name_w}}"
    print(line)
    print("-" * len(line))
    for r in rows:
        print(
            f"{r['method']:<{method_w}}  {r['path']:<{path_w}}  {(r.get('name') or ''):<{name_w}}"
        )
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """`veloce check` — run a pre-deploy security audit of the app."""
    app = _load_app(args.app)
    _require_app_attr(app, "security_audit", "`.security_audit()`")

    warnings = app.security_audit()
    if not warnings:
        print("Security audit: no issues found.")
        return 0
    print(f"Security audit: {len(warnings)} issue(s) found:")
    for warning in warnings:
        print(f"  - {warning}")
    return 1


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
    We therefore peel the tail off ourselves — greedily consuming only the
    env-file flags after `app` — and let argparse parse just the head. An
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
    # value token, so skip that value too — otherwise PATH is mistaken for
    # the app reference and the real reference is pushed into the tail.
    idx = 1
    while idx < len(argv) and argv[idx].startswith("-") and argv[idx] != "--":
        if argv[idx] == "--env-file":
            idx += 2  # skip the flag and its value
        else:
            idx += 1
    if idx >= len(argv):
        return argv, None  # no `app` — let argparse report the error
    head = argv[: idx + 1]  # ["custom", ..., "app"]
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


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser. Exposed for testing."""
    parser = _VeloceArgumentParser(
        prog="veloce",
        description="Veloce — ultra-fast async Python web framework.",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"veloce {_resolve_version()}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the app under uvicorn.")
    p_run.add_argument("app", help="App reference in 'module:attribute' form.")
    p_run.add_argument("--host", default="127.0.0.1")
    p_run.add_argument("--port", type=int, default=8000)
    p_run.add_argument("--reload", action="store_true", help="Auto-reload on code changes.")
    p_run.add_argument("--workers", type=int, default=1)
    p_run.add_argument("--log-level", default="info")
    _add_env_file_args(p_run)
    p_run.set_defaults(func=_cmd_run)

    p_routes = sub.add_parser("routes", help="Print the route table.")
    p_routes.add_argument("app", help="App reference in 'module:attribute' form.")
    p_routes.set_defaults(func=_cmd_routes)

    p_check = sub.add_parser("check", help="Run a pre-deploy security audit.")
    p_check.add_argument("app", help="App reference in 'module:attribute' form.")
    p_check.set_defaults(func=_cmd_check)

    p_shell = sub.add_parser("shell", help="Interactive Python shell with the app loaded.")
    p_shell.add_argument("app", help="App reference in 'module:attribute' form.")
    _add_env_file_args(p_shell)
    p_shell.set_defaults(func=_cmd_shell)

    p_custom = sub.add_parser(
        "custom",
        help="Run an app.cli (Click) command defined on the app.",
    )
    p_custom.add_argument("app", help="App reference in 'module:attribute' form.")
    _add_env_file_args(p_custom)
    # The forwarded Click argv is peeled off after a literal `--` by
    # `_VeloceArgumentParser.parse_known_args`; argparse only ever sees the
    # head. Default to no extra args when `--` is absent.
    p_custom.set_defaults(func=_cmd_custom, cli_args=[])

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the veloce CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
