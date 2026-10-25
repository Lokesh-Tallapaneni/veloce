"""Auto-reloader — restart the built-in server when source files change.

For `Veloce.run(reload=True)`: the launched process becomes a supervisor that
spawns the real server in a child and re-spawns it when a watched `.py` file
changes. The watching runs only in the supervisor, so a served app pays nothing.
The default watcher is a stdlib `os.stat` mtime poller; `watchfiles`, when
installed, is used instead for OS-level change events.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any, cast

try:
    import watchfiles
except ImportError:  # pragma: no cover - exercised only without the extra
    # Probed once here rather than per change; `None` selects the stat poller.
    watchfiles = cast("Any", None)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Iterator

# "veloce.reloader" is the documented name users filter on, hence the literal.
_logger = logging.getLogger("veloce.reloader")

# Set in the child's environment so it serves instead of supervising in turn.
_CHILD_ENV = "VELOCE_RUN_RELOADER"

# Build artifacts, VCS metadata and virtualenvs dominate a stat poll otherwise.
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        ".tox",
        "site",
    }
)

_POLL_INTERVAL = 1.0


def is_reloader_child() -> bool:
    """Return whether this process is the reloader-spawned worker."""
    return os.environ.get(_CHILD_ENV) == "true"


def _restart_command() -> list[str]:
    """Rebuild the command that launched this process, for re-spawning.

    Covers a plain script, a console-script / executable launcher (which must be
    run directly, not handed back to ``python``), and `python -m pkg`.
    Interactive (`-c` / REPL) launches are not reloadable.
    """
    main = sys.modules["__main__"]
    script = sys.argv[0]
    rest = sys.argv[1:]
    package = getattr(main, "__package__", None)
    on_windows = os.name == "nt"

    # On Windows argv[0] can be the extension-less name beside the `.exe`.
    launched_as_file = package is None or (
        on_windows
        and package == ""
        and not os.path.exists(script)
        and os.path.exists(script + ".exe")
    )
    if launched_as_file:
        path = os.path.abspath(script)
        if on_windows and not os.path.exists(path) and os.path.exists(path + ".exe"):
            path += ".exe"
        # An executable launcher runs directly; only a source file goes via python.
        if on_windows and path.lower().endswith(".exe"):
            return [path, *rest]
        return [sys.executable, path, *rest]

    # `python -m pkg` rewrote argv[0] to the module's file; rebuild the `-m` form.
    if os.path.isfile(script):
        module = package or ""
        stem = os.path.splitext(os.path.basename(script))[0]
        if stem != "__main__":
            module = f"{module}.{stem}" if module else stem
    else:
        module = script
    return [sys.executable, "-m", module.lstrip("."), *rest]


def _iter_source_files(dirs: Iterable[str]) -> Iterator[str]:
    """Yield watchable `.py` files under `dirs`, skipping noise directories."""
    for directory in dirs:
        for root, subdirs, files in os.walk(directory):
            # Prune in place so os.walk does not descend into skipped trees.
            subdirs[:] = [d for d in subdirs if d not in _SKIP_DIRS]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(root, name)


def _snapshot(dirs: Iterable[str]) -> dict[str, float]:
    """Map each watched file to its modification time."""
    snap: dict[str, float] = {}
    for path in _iter_source_files(dirs):
        try:
            snap[path] = os.stat(path).st_mtime
        except OSError:
            # Its absence is itself a change the next comparison will catch.
            continue
    return snap


def _changed_path(before: dict[str, float], after: dict[str, float]) -> str | None:
    """Return one path that was added, removed, or modified, else None."""
    for path, mtime in after.items():
        if before.get(path) != mtime:
            return path
    for path in before:
        if path not in after:
            return path
    return None


def _wait_for_change_stat(dirs: list[str], interval: float) -> str:
    """Block until a watched file changes, returning its path (stdlib poller)."""
    baseline = _snapshot(dirs)
    while True:
        time.sleep(interval)
        current = _snapshot(dirs)
        changed = _changed_path(baseline, current)
        if changed is not None:
            return changed
        baseline = current


def _wait_for_change_watchfiles(dirs: list[str]) -> str:
    """Block until a `.py` file changes, returning its path (watchfiles)."""
    for batch in watchfiles.watch(*dirs):
        for _change, path in batch:
            if path.endswith(".py"):
                return path
    # Unreachable - watch() stops only on a stop event we never set.
    return ""  # pragma: no cover


def _wait_for_change(dirs: list[str], interval: float) -> str:
    """Block until a watched source file changes; pick the best backend."""
    if watchfiles is None:
        return _wait_for_change_stat(dirs, interval)
    return _wait_for_change_watchfiles(dirs)


def _announce(message: str) -> None:
    """Emit one supervisor event to the console and the `veloce.reloader` logger.

    From one string, so the two accounts of the event cannot drift apart.
    """
    _logger.info(message)
    print(f"  {message}\n")


def _terminate(child: subprocess.Popen[bytes]) -> None:
    """Stop the worker, escalating to a hard kill if it will not exit."""
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def run_with_reloader(
    watch_dirs: list[str] | None = None,
    *,
    interval: float = _POLL_INTERVAL,
) -> int:
    """Supervise the server, restarting it on source changes.

    Spawns the server in a child process, watches `watch_dirs` (defaulting to the
    current working directory), and re-spawns on every change until interrupted.
    Returns 0 once interrupted (Ctrl+C or SIGTERM). Runs only in the supervisor
    process; the child serves requests with no watcher attached.
    """
    dirs = watch_dirs if watch_dirs else [os.getcwd()]
    command = _restart_command()
    child_env = {**os.environ, _CHILD_ENV: "true"}

    _announce(f"Reloader active - watching {dirs[0]} for changes")

    # Routes SIGTERM into the Ctrl+C path so the serving child is never orphaned.
    def _request_stop(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    # signal.signal() returns None for a C-installed prior handler, so a
    # `previous_term is not None` guard would skip a real restore.
    installed = False
    previous_term: Any = None
    with contextlib.suppress(ValueError, OSError):
        previous_term = signal.signal(signal.SIGTERM, _request_stop)
        installed = True

    child: subprocess.Popen[bytes] | None = None
    try:
        while True:
            child = subprocess.Popen(command, env=child_env)
            changed = _wait_for_change(dirs, interval)
            _announce(f"Change detected in {os.path.basename(changed)} - reloading")
            _terminate(child)
    except KeyboardInterrupt:
        return 0
    finally:
        if installed:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, previous_term)
        if child is not None:
            _terminate(child)
