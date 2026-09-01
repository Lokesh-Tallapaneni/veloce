"""`background=` means the same thing on every response type."""

from __future__ import annotations

import pytest

from veloce import Response, Veloce
from veloce.background import BackgroundTask, BackgroundTasks, coerce_background
from veloce.testclient import TestClient


def test_a_bare_callable_background_actually_runs():
    """POSITIVE: the documented `TemplateResponse` shape works here too."""
    ran: list[str] = []
    app = Veloce()

    @app.get("/go")
    async def go():
        return Response(body=b"ok", background=lambda: ran.append("ran"))

    with TestClient(app) as client:
        assert client.get("/go").status_code == 200

    assert ran == ["ran"]


def test_an_unsupported_background_is_refused_at_construction():
    """NEGATIVE: the silent drop becomes a loud failure where the mistake was made."""
    with pytest.raises(TypeError, match="background must be"):
        Response(body=b"ok", background=object())


def test_a_background_task_is_passed_through_unchanged():
    """POSITIVE: the documented type is not re-wrapped."""
    task = BackgroundTask(lambda: None)
    assert coerce_background(task) is task


def test_a_background_tasks_collection_is_passed_through_unchanged():
    """POSITIVE: the `run_all` shape is recognised too."""
    tasks = BackgroundTasks()
    assert coerce_background(tasks) is tasks


def test_none_stays_none():
    """POSITIVE: no task attached remains the free path."""
    assert coerce_background(None) is None


def test_a_bare_callable_is_wrapped_in_a_task():
    """POSITIVE: wrapping is what makes the dispatch cascade recognise it."""
    coerced = coerce_background(lambda: None)
    assert isinstance(coerced, BackgroundTask)


def test_junk_is_refused_by_the_helper_itself():
    """NEGATIVE: the helper is the gate, not just the Response constructor."""
    with pytest.raises(TypeError, match="background must be"):
        coerce_background(42)
