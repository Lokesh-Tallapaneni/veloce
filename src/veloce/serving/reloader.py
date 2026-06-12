"""Auto-reloader — restart the built-in server when source files change.

A development convenience for `Veloce.run(reload=True)` (and `veloce run
--reload` without uvicorn). The launched process becomes a *supervisor* that
spawns the real server in a child subprocess and watches the project's `.py`
files; on a change it terminates and re-spawns the child, so edited code is
picked up without a manual restart. This mirrors the supervisor/child pattern
Werkzeug and uvicorn use, written from scratch for Veloce.

It runs only when reload is explicitly requested, and the file-watching happens
in the supervisor process, never in the child that serves requests - so a served
app pays nothing for it. The default watcher is a stdlib `os.stat` mtime poller
(no dependency, works on every platform); when `watchfiles` is installed it is
used instead for OS-level change events.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Iterator

_logger = logging.getLogger("veloce.reloader")

# Set by the supervisor in the child's environment so the child serves directly
# instead of recursing into another supervisor. Its presence is the one signal
# that distinguishes "I am the worker" from "I am the watcher".
_CHILD_ENV = "VELOCE_RUN_RELOADER"

# Directory names never worth walking - build artifacts, VCS metadata, and
# virtualenvs dominate a stat poll otherwise and never carry app source.
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

# Poll cadence for the stdlib watcher. Fast enough to feel instant, slow enough
# that walking the tree is negligible on the supervisor process.
_POLL_INTERVAL = 1.0


def is_reloader_child() -> bool:
    """Return whether this process is the reloader-spawned worker."""
    return os.environ.get(_CHILD_ENV) == "true"


def _restart_command() -> list[str]:
    """Rebuild the command that launched this process, for re-spawning."""
    # Re-running `sys.executable` with the original argv reproduces both the
    # `veloce run ... --reload` console-script invocation and a plain
    # `python app.py`. Interactive (`-c`, REPL) launches are not reloadable.
    return [sys.executable, *sys.argv]


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
            # A file deleted mid-walk simply drops out of the snapshot; its
            # absence is itself a change the next comparison will catch.
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
    import watchfiles

    for batch in watchfiles.watch(*dirs):
        for _change, path in batch:
            if path.endswith(".py"):
                return path
    # watch() only stops if its internal stop event fires, which we never set;
    # this is unreachable but keeps the return type honest.
    return ""  # pragma: no cover


def _wait_for_change(dirs: list[str], interval: float) -> str:
    """Block until a watched source file changes; pick the best backend."""
    try:
        import watchfiles  # noqa: F401
    except ImportError:
        return _wait_for_change_stat(dirs, interval)
    return _wait_for_change_watchfiles(dirs)


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
    Returns the last child's exit code. Runs only in the supervisor process; the
    child serves requests with no watcher attached.
    """
    dirs = watch_dirs if watch_dirs else [os.getcwd()]
    command = _restart_command()
    child_env = {**os.environ, _CHILD_ENV: "true"}

    print(f"  Reloader active - watching {dirs[0]} for changes\n")

    # Route a graceful kill (SIGTERM, e.g. from a process manager) through the
    # same cleanup path as Ctrl+C so the serving child is never orphaned.
    # Best-effort: signal handlers only install on the main thread, and a
    # Windows hard kill is not catchable - Ctrl+C still cleans up there.
    def _request_stop(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    previous_term: Any = None
    with contextlib.suppress(ValueError, OSError):
        previous_term = signal.signal(signal.SIGTERM, _request_stop)

    child: subprocess.Popen[bytes] | None = None
    try:
        while True:
            child = subprocess.Popen(command, env=child_env)
            changed = _wait_for_change(dirs, interval)
            _logger.info("change detected in %s - reloading", os.path.basename(changed))
            print(f"  Change in {os.path.basename(changed)} - reloading\n")
            _terminate(child)
    except KeyboardInterrupt:
        return 0
    finally:
        if previous_term is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, previous_term)
        if child is not None:
            _terminate(child)
