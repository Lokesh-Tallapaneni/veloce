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
import os
import sys
from typing import Any


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


def _cmd_run(args: argparse.Namespace) -> int:
    """`veloce run` — hand the app off to uvicorn."""
    try:
        import uvicorn
    except ImportError as err:  # pragma: no cover — only on broken envs
        raise SystemExit("uvicorn is not installed. Install it with: pip install uvicorn") from err

    uvicorn.run(
        args.app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers if args.workers > 1 else None,
        log_level=args.log_level,
    )
    return 0


def _cmd_shell(args: argparse.Namespace) -> int:
    """`veloce shell` — drop into a Python REPL with the app loaded.

    Surfaces `app` and `g` plus anything `@app.shell_context_processor`
    contributes. Uses `code.interact` so the shell is stdlib-only — no
    IPython dependency. Inside an `app_context()` so `current_app` and
    `g` resolve as if a request were active.
    """
    import code

    app = _load_app(args.app)
    if not hasattr(app, "make_shell_context"):
        raise SystemExit(f"{args.app} is not a Veloce app (missing `.make_shell_context`)")

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
    app = _load_app(args.app)
    if not hasattr(app, "cli"):
        raise SystemExit(f"{args.app} is not a Veloce app (missing `.cli`)")
    with app.app_context():
        # `app.cli` is a `click.Group`. Call it with the remaining argv
        # and `standalone_mode=False` so we own the exit code path.
        try:
            return int(app.cli.main(args.cli_args, standalone_mode=False) or 0)
        except SystemExit as exc:  # Click raises on --help etc.
            return int(exc.code or 0)


def _cmd_routes(args: argparse.Namespace) -> int:
    """`veloce routes` — print the route table."""
    app = _load_app(args.app)
    if not hasattr(app, "routes"):
        raise SystemExit(f"{args.app} is not a Veloce app (missing `.routes` property)")

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


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser. Exposed for testing."""
    parser = argparse.ArgumentParser(
        prog="veloce",
        description="Veloce — ultra-fast async Python web framework.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the app under uvicorn.")
    p_run.add_argument("app", help="App reference in 'module:attribute' form.")
    p_run.add_argument("--host", default="127.0.0.1")
    p_run.add_argument("--port", type=int, default=8000)
    p_run.add_argument("--reload", action="store_true", help="Auto-reload on code changes.")
    p_run.add_argument("--workers", type=int, default=1)
    p_run.add_argument("--log-level", default="info")
    p_run.set_defaults(func=_cmd_run)

    p_routes = sub.add_parser("routes", help="Print the route table.")
    p_routes.add_argument("app", help="App reference in 'module:attribute' form.")
    p_routes.set_defaults(func=_cmd_routes)

    p_shell = sub.add_parser("shell", help="Interactive Python shell with the app loaded.")
    p_shell.add_argument("app", help="App reference in 'module:attribute' form.")
    p_shell.set_defaults(func=_cmd_shell)

    p_custom = sub.add_parser(
        "custom",
        help="Run an app.cli (Click) command defined on the app.",
    )
    p_custom.add_argument("app", help="App reference in 'module:attribute' form.")
    p_custom.add_argument(
        "cli_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to the app's Click group.",
    )
    p_custom.set_defaults(func=_cmd_custom)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
