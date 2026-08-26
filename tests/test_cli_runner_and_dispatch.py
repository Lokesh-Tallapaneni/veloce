"""Veloce.test_cli_runner + Veloce.dispatch_request / full_dispatch_request."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce


def _req(path: str = "/x") -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── test_cli_runner ──────────────────────────────────────────────────


def test_test_cli_runner_returns_click_runner():
    pytest.importorskip("click")
    from click.testing import CliRunner

    app = Veloce(openapi_url=None)
    runner = app.test_cli_runner()
    assert isinstance(runner, CliRunner)


def test_test_cli_runner_drives_app_cli_command():
    pytest.importorskip("click")

    app = Veloce(openapi_url=None)

    @app.cli.command("ping")
    def ping():
        import click

        click.echo("pong")

    runner = app.test_cli_runner()
    result = runner.invoke(app.cli, ["ping"])
    assert result.exit_code == 0
    assert "pong" in result.output


# ── dispatch_request / full_dispatch_request ─────────────────────────


@pytest.mark.asyncio
async def test_dispatch_request_alias_runs_handler():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {"hit": True}

    resp = await app.dispatch_request(_req())
    import orjson

    assert orjson.loads(resp.body) == {"hit": True}


@pytest.mark.asyncio
async def test_full_dispatch_request_alias_runs_handler():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {"hit": True}

    resp = await app.full_dispatch_request(_req())
    import orjson

    assert orjson.loads(resp.body) == {"hit": True}
