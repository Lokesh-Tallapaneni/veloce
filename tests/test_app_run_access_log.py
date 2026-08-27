"""`run(access_log=True)` produces an access log, not only a banner.

The flag is named after the access log and, until now, printed the startup
banner and nothing else — the built-in server answered every request silently.
A development server is expected to say what it served, and the silence matters
most exactly when something is wrong: a failing endpoint left nothing to
correlate a report against.

It is installed for the built-in server only. Under an ASGI server that server
writes the access log, and a second one would duplicate every line.
"""

from __future__ import annotations

import logging

from veloce import Veloce
from veloce.observability import instrument_access_log
from veloce.testclient import TestClient


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    return app


def test_the_dev_server_installs_an_access_log():
    app = _app()
    assert app._instrumentation == []
    app._install_dev_access_log()
    assert len(app._instrumentation) == 1


def test_it_does_not_double_up_on_an_app_that_installed_its_own():
    """`run()` must not add a second line to every request."""
    app = _app()
    instrument_access_log(app)
    app._install_dev_access_log()
    assert len(app._instrumentation) == 1


def test_installing_twice_is_a_no_op():
    app = _app()
    app._install_dev_access_log()
    app._install_dev_access_log()
    assert len(app._instrumentation) == 1


def test_an_applications_own_instrumentation_is_not_mistaken_for_one():
    """A user hook is unrelated; the access log must still be installed."""
    app = _app()

    def my_metrics(metrics) -> None:
        return None

    app.add_instrumentation(my_metrics)
    app._install_dev_access_log()
    assert len(app._instrumentation) == 2


def test_a_served_request_produces_a_line(caplog):
    app = _app()
    app._install_dev_access_log()
    with caplog.at_level(logging.INFO, logger="veloce.access"):
        TestClient(app).get("/ping")
    assert any("GET" in r.getMessage() and "/ping" in r.getMessage() for r in caplog.records)


def test_the_line_carries_the_status_code(caplog):
    app = _app()
    app._install_dev_access_log()
    with caplog.at_level(logging.INFO, logger="veloce.access"):
        TestClient(app).get("/ping")
    assert any("200" in r.getMessage() for r in caplog.records)


def test_a_failing_request_is_still_logged(caplog):
    """The case the missing log hurt most."""
    app = Veloce(openapi_url=None)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom-marker")

    app._install_dev_access_log()
    with caplog.at_level(logging.INFO):
        TestClient(app).get("/boom")
    messages = [r.getMessage() for r in caplog.records]
    assert any("500" in m for m in messages)


# ── the access log asks the hook ─────────────────────────────────────
#
# Moved here from `test_extensibility_gaps.py`, a module named for a review
# batch rather than a subject.


def test_a_user_access_log_suppresses_the_built_in_one():
    """The defect: identified by `__module__`, so only Veloce's own counted."""
    app = Veloce(openapi_url=None)

    def my_access_log(metrics):
        pass

    my_access_log.is_access_log = True
    app.add_instrumentation(my_access_log)
    app._install_dev_access_log()
    assert app._instrumentation == [my_access_log]


def test_an_unmarked_hook_does_not_suppress_it():
    app = Veloce(openapi_url=None)

    def timing(metrics):
        pass

    app.add_instrumentation(timing)
    app._install_dev_access_log()
    assert len(app._instrumentation) == 2


def test_the_built_in_access_log_is_installed_once():
    app = Veloce(openapi_url=None)
    app._install_dev_access_log()
    app._install_dev_access_log()
    assert len(app._instrumentation) == 1


def test_the_built_in_hook_carries_the_marker():
    """So a second installer can recognise it the same way."""
    app = Veloce(openapi_url=None)
    app._install_dev_access_log()
    assert getattr(app._instrumentation[0], "is_access_log", False) is True


class TestTheFlagIsActuallyWired:
    """`run(access_log=...)` reaches the installer, which nothing was checking.

    Every test above calls `_install_dev_access_log()` directly, so the branch
    in `run()` that connects the public flag to it - and the banner that shares
    the branch - was never exercised. `run()` binds a socket and blocks, so it
    is driven with `_serve` stubbed out: the flag's effect is decided before
    serving starts.
    """

    @staticmethod
    def _run_without_serving(app: Veloce, monkeypatch, **kwargs) -> None:
        async def _no_serve(*args, **kwargs):
            return None

        monkeypatch.setattr(app, "_serve", _no_serve)
        monkeypatch.setattr(app, "_graceful_shutdown", _no_serve)
        app.run(**kwargs)

    def test_the_flag_on_installs_the_access_log(self, monkeypatch, capsys):
        app = _app()
        assert app._instrumentation == []
        self._run_without_serving(app, monkeypatch, access_log=True)
        assert len(app._instrumentation) == 1
        assert getattr(app._instrumentation[0], "is_access_log", False)

    def test_the_flag_off_installs_nothing(self, monkeypatch, capsys):
        app = _app()
        self._run_without_serving(app, monkeypatch, access_log=False)
        assert app._instrumentation == []

    def test_the_flag_on_also_prints_the_banner(self, monkeypatch, capsys):
        app = _app()
        self._run_without_serving(app, monkeypatch, access_log=True)
        assert "Listening on" in capsys.readouterr().out

    def test_the_flag_off_prints_no_banner(self, monkeypatch, capsys):
        app = _app()
        self._run_without_serving(app, monkeypatch, access_log=False)
        assert "Listening on" not in capsys.readouterr().out

    def test_running_twice_does_not_install_two(self, monkeypatch, capsys):
        """The de-duplication, reached through the public flag rather than direct."""
        app = _app()
        self._run_without_serving(app, monkeypatch, access_log=True)
        self._run_without_serving(app, monkeypatch, access_log=True)
        assert len(app._instrumentation) == 1
