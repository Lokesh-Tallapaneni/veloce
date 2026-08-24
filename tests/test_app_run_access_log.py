"""`run(access_log=True)` produces an access log, not only a banner.

The flag is named after the access log and, until now, printed the startup
banner and nothing else — the built-in server answered every request silently.
That is out of step with both peers (uvicorn and Flask's dev server log each
request), and it matters most exactly when something is wrong: a failing
endpoint left nothing to correlate a report against.

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
