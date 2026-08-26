"""A `TestClient` used without its context manager still hands the app back.

`TestClient.__init__` disables the app's setup lock so a test can register
routes after construction, and `close()` puts it back. Around 959 of the ~1456
client constructions in this suite are bare - no `with` - so `close()` never ran
and the app was left with its setup-lock protection **off for good**:

    client = TestClient(app)      # app._setup_lock_enabled -> False
    ...                           # never restored

A later "registering after serving is refused" check on that app would then
silently not be checking anything - the guard it relies on is disabled.

The finaliser now restores the lock. That is a plain attribute assignment, which
is safe there; the async shutdown lifecycle is not, and still needs `close()` or
the context manager - so this closes the state leak, not the shutdown-hook gap.
"""

from __future__ import annotations

import gc

import pytest

from veloce import Veloce
from veloce.testclient import TestClient


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    return app


# ── the state is handed back either way ──────────────────────────────


def test_the_context_manager_restores_the_setup_lock():
    app = _app()
    before = app._setup_lock_enabled
    with TestClient(app) as client:
        client.get("/")
    assert app._setup_lock_enabled == before


def test_a_bare_client_restores_the_setup_lock_when_collected():
    """The defect: this stayed `False` for the app's lifetime."""
    app = _app()
    before = app._setup_lock_enabled

    client = TestClient(app)
    client.get("/")
    assert app._setup_lock_enabled is False, "the client should disable it while alive"

    del client
    gc.collect()
    assert app._setup_lock_enabled == before


def test_the_lock_is_disabled_while_the_client_is_alive():
    """The negative: restoring too eagerly would break the reason it is
    disabled - registering a route after constructing the client."""
    app = _app()
    client = TestClient(app)

    async def late():
        return {"late": True}

    app.add_route("/late", late, methods=["GET"])
    assert client.get("/late").json() == {"late": True}


def test_closing_explicitly_still_restores_it():
    app = _app()
    before = app._setup_lock_enabled
    client = TestClient(app)
    client.get("/")
    client.close()
    assert app._setup_lock_enabled == before


def test_restoring_twice_is_harmless():
    """`close()` then collection must not put back a stale value."""
    app = _app()
    before = app._setup_lock_enabled
    client = TestClient(app)
    client.close()
    app._setup_lock_enabled = False  # something else disables it afterwards
    del client
    gc.collect()
    assert app._setup_lock_enabled is False, "the finaliser re-applied a stale value"
    app._setup_lock_enabled = before


# ── and the shutdown lifecycle still needs the context manager ───────
#
# Stated so the remaining gap is explicit rather than assumed closed: the
# finaliser cannot run async work, so a bare client still does not fire
# shutdown hooks. That is why the context manager is the documented form.


def test_the_context_manager_runs_shutdown_hooks():
    ran: list[str] = []
    app = _app()

    @app.on_shutdown
    async def down():
        ran.append("shutdown")

    with TestClient(app) as client:
        client.get("/")
    assert ran == ["shutdown"]


def test_a_bare_client_does_not_run_shutdown_hooks():
    """Not a defect being fixed here - a limitation being pinned. A finaliser
    cannot await."""
    ran: list[str] = []
    app = _app()

    @app.on_shutdown
    async def down():
        ran.append("shutdown")

    client = TestClient(app)
    client.get("/")
    del client
    gc.collect()
    assert ran == []


def test_close_runs_shutdown_hooks_once():
    ran: list[str] = []
    app = _app()

    @app.on_shutdown
    async def down():
        ran.append("shutdown")

    client = TestClient(app)
    client.get("/")
    client.close()
    client.close()
    assert ran == ["shutdown"]


@pytest.mark.parametrize("form", ["with", "close"])
def test_both_documented_forms_shut_the_app_down(form):
    ran: list[str] = []
    app = _app()

    @app.on_shutdown
    async def down():
        ran.append("shutdown")

    if form == "with":
        with TestClient(app) as client:
            client.get("/")
    else:
        client = TestClient(app)
        client.get("/")
        client.close()
    assert ran == ["shutdown"]
