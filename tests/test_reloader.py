"""Development auto-reloader for the built-in server (`run(reload=True)`).

The supervisor watches project `.py` files and re-spawns the serving child on a
change. These tests cover the watch/snapshot logic, the supervisor loop, and the
`run()` wiring deterministically - without binding sockets or sleeping on real
file-system latency.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from veloce import Veloce
from veloce.serving import reloader

# ── child detection ───────────────────────────────────────────────────


def test_is_reloader_child_reads_env(monkeypatch):
    monkeypatch.delenv("VELOCE_RUN_RELOADER", raising=False)
    assert reloader.is_reloader_child() is False
    monkeypatch.setenv("VELOCE_RUN_RELOADER", "true")
    assert reloader.is_reloader_child() is True


# ── file discovery ────────────────────────────────────────────────────


def test_iter_source_files_skips_noise_and_non_python(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "notes.txt").write_text("hi\n")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("y = 2\n")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "stale.py").write_text("z = 3\n")

    found = {
        f.replace("\\", "/").rsplit("/", 1)[-1]
        for f in reloader._iter_source_files([str(tmp_path)])
    }
    assert found == {"app.py", "mod.py"}  # .txt excluded, __pycache__ pruned


# ── change detection ──────────────────────────────────────────────────


def test_changed_path_detects_modify_add_remove():
    before = {"a.py": 1.0, "b.py": 2.0}
    assert reloader._changed_path(before, before) is None
    assert reloader._changed_path(before, {"a.py": 1.0, "b.py": 9.0}) == "b.py"  # modified
    assert (
        reloader._changed_path(before, {"a.py": 1.0, "b.py": 2.0, "c.py": 3.0}) == "c.py"
    )  # added
    assert reloader._changed_path(before, {"a.py": 1.0}) == "b.py"  # removed


def test_wait_for_change_stat_returns_on_new_file(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    result: dict[str, str] = {}

    def watch():
        result["path"] = reloader._wait_for_change_stat([str(tmp_path)], interval=0.02)

    worker = threading.Thread(target=watch, daemon=True)
    worker.start()
    time.sleep(0.1)
    # Adding a file is detected without depending on mtime resolution.
    (tmp_path / "new.py").write_text("y = 2\n")
    worker.join(timeout=5)

    assert not worker.is_alive(), "watcher did not return after a change"
    assert result["path"].endswith("new.py")


# ── restart command ───────────────────────────────────────────────────


def test_restart_command_reproduces_invocation(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["veloce", "run", "app:app", "--reload"])
    assert reloader._restart_command() == [sys.executable, "veloce", "run", "app:app", "--reload"]


# ── supervisor loop ───────────────────────────────────────────────────


def test_run_with_reloader_restarts_then_exits_on_interrupt(monkeypatch):
    # A harmless long-lived child stands in for the server.
    monkeypatch.setattr(
        reloader,
        "_restart_command",
        lambda: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    children: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def tracking_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(reloader.subprocess, "Popen", tracking_popen)

    calls = {"n": 0}

    def fake_wait(dirs, interval):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt  # second wait ends the loop
        return "app.py"

    monkeypatch.setattr(reloader, "_wait_for_change", fake_wait)

    rc = reloader.run_with_reloader(["."], interval=0.01)

    assert rc == 0
    assert len(children) == 2, "should spawn, restart once, then stop"
    for proc in children:
        assert proc.poll() is not None, "every child must be terminated, no leaks"


# ── run() wiring ──────────────────────────────────────────────────────


def test_run_reload_enters_supervisor_and_returns(monkeypatch):
    app = Veloce()
    called = {"n": 0}
    monkeypatch.setattr(reloader, "is_reloader_child", lambda: False)
    monkeypatch.setattr(reloader, "run_with_reloader", lambda *a, **k: called.__setitem__("n", 1))

    app.run(reload=True)  # must hand off to the supervisor, never bind a socket

    assert called["n"] == 1


def test_run_reload_child_skips_supervisor_and_serves(monkeypatch):
    app = Veloce()
    monkeypatch.setattr(reloader, "is_reloader_child", lambda: True)
    monkeypatch.setattr(
        reloader, "run_with_reloader", lambda *a, **k: pytest.fail("child must not supervise")
    )

    # A child falls through the reload branch to the serving path; stop it at the
    # first serving step so the test never opens a socket.
    sentinel = RuntimeError("reached serving setup")

    def boom(self):
        raise sentinel

    monkeypatch.setattr(type(app), "_setup_openapi", boom)

    with pytest.raises(RuntimeError, match="reached serving setup"):
        app.run(reload=True)
