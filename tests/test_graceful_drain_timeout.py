"""How long shutdown waits for in-flight requests belongs to the deployment.

`HttpProtocol` has three shutdown-adjacent budgets. Two were config-driven. The
third — how long shutdown waits for in-flight dispatch tasks after every
connection has been asked to quiesce — was the literal `30` in the caller,
reachable by no setting.

It has to fit inside the orchestrator's termination grace period, which the
framework cannot know. A container with a ten-second grace was SIGKILLed
mid-drain and no operator setting could change that.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce import Veloce
from veloce.config import Config
from veloce.serving.protocol import HttpProtocol


class _FakeTask:
    """Stands in for an in-flight dispatch task; shutdown cancels stragglers."""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


async def _shutdown(app: Veloce) -> None:
    """Drive the shutdown from inside a running loop, as the server does."""
    await app._graceful_shutdown(asyncio.get_running_loop())


# ── the key exists and is honoured ───────────────────────────────────


def test_the_drain_budget_has_a_default():
    assert Config.default_config()["GRACEFUL_DRAIN_TIMEOUT"] == 30


def test_the_default_preserves_the_old_behaviour():
    """The literal it replaced was 30, so no deployment changes by upgrading."""
    assert Veloce(openapi_url=None).config["GRACEFUL_DRAIN_TIMEOUT"] == 30


@pytest.mark.parametrize("configured", [1, 5, 120])
def test_the_configured_budget_is_what_shutdown_waits(monkeypatch, configured):
    """The defect: this waited 30 whatever the setting said."""
    app = Veloce(openapi_url=None)
    app.config["GRACEFUL_DRAIN_TIMEOUT"] = configured
    seen: list[float | None] = []

    async def fake_wait(tasks, timeout=None):
        seen.append(timeout)
        return set(), set()

    monkeypatch.setattr(asyncio, "wait", fake_wait)
    monkeypatch.setattr(HttpProtocol, "_active_tasks", {_FakeTask()}, raising=False)
    asyncio.run(_shutdown(app))
    assert seen == [configured]


def test_an_unset_key_falls_back_to_the_shipped_default(monkeypatch):
    """A config built by hand need not carry every key."""
    app = Veloce(openapi_url=None)
    del app.config["GRACEFUL_DRAIN_TIMEOUT"]
    seen: list[float | None] = []

    async def fake_wait(tasks, timeout=None):
        seen.append(timeout)
        return set(), set()

    monkeypatch.setattr(asyncio, "wait", fake_wait)
    monkeypatch.setattr(HttpProtocol, "_active_tasks", {_FakeTask()}, raising=False)
    asyncio.run(_shutdown(app))
    assert seen == [30]


def test_the_budget_is_typed_from_an_env_file(tmp_path):
    """It is a number, so an env file's string must not reach `asyncio.wait`."""
    env = tmp_path / ".env"
    env.write_text("GRACEFUL_DRAIN_TIMEOUT=5\n", encoding="utf-8")
    app = Veloce(openapi_url=None)
    app.config.from_env_file(str(env))
    assert app.config["GRACEFUL_DRAIN_TIMEOUT"] == 5


# ── it is a separate budget from the background-task one ─────────────


def test_the_two_shutdown_budgets_are_distinct_keys():
    """They run in sequence, so an operator has to be able to set each."""
    defaults = Config.default_config()
    assert defaults["GRACEFUL_TASK_TIMEOUT"] == 10
    assert defaults["GRACEFUL_DRAIN_TIMEOUT"] == 30
    assert "GRACEFUL_TASK_TIMEOUT" != "GRACEFUL_DRAIN_TIMEOUT"


def test_changing_one_does_not_change_the_other():
    app = Veloce(openapi_url=None)
    app.config["GRACEFUL_DRAIN_TIMEOUT"] = 1
    assert app.config["GRACEFUL_TASK_TIMEOUT"] == 10


# ── shutdown still does the rest of its job ──────────────────────────


def test_shutdown_cancels_a_straggler_after_the_window(monkeypatch):
    """The window bounds the wait; it must not stop the cancel that follows."""

    task = _FakeTask()
    app = Veloce(openapi_url=None)
    app.config["GRACEFUL_DRAIN_TIMEOUT"] = 0

    async def fake_wait(tasks, timeout=None):
        return set(), set(tasks)

    monkeypatch.setattr(asyncio, "wait", fake_wait)
    monkeypatch.setattr(HttpProtocol, "_active_tasks", {task}, raising=False)
    asyncio.run(_shutdown(app))
    assert task.cancelled


def test_shutdown_with_no_in_flight_tasks_does_not_wait(monkeypatch):
    called: list[object] = []

    async def fake_wait(tasks, timeout=None):
        called.append(tasks)
        return set(), set()

    monkeypatch.setattr(asyncio, "wait", fake_wait)
    monkeypatch.setattr(HttpProtocol, "_active_tasks", set(), raising=False)
    asyncio.run(_shutdown(Veloce(openapi_url=None)))
    assert called == []


def test_shutdown_clears_the_drain_latch(monkeypatch):
    """A single interpreter that serves again must not inherit `draining`."""
    from veloce.serving import protocol as protocol_module

    async def fake_wait(tasks, timeout=None):
        return set(), set()

    monkeypatch.setattr(asyncio, "wait", fake_wait)
    monkeypatch.setattr(HttpProtocol, "_active_tasks", set(), raising=False)
    asyncio.run(_shutdown(Veloce(openapi_url=None)))
    assert protocol_module._SHUTTING_DOWN is False


# ── the keep-alive default is declared once ──────────────────────────


def test_the_keep_alive_default_is_not_written_twice():
    """Two copies of one number are two lines to keep in step."""
    assert Config.default_config()["KEEP_ALIVE_TIMEOUT"] == HttpProtocol.KEEP_ALIVE_TIMEOUT


def test_a_configured_keep_alive_still_wins_over_the_class_fallback():
    app = Veloce(openapi_url=None)
    app.config["KEEP_ALIVE_TIMEOUT"] = 5
    assert app.config.get("KEEP_ALIVE_TIMEOUT", HttpProtocol.KEEP_ALIVE_TIMEOUT) == 5
