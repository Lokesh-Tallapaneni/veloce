"""`veloce mcp` — serving and inspecting an app's MCP surface from the CLI.

An MCP client launches its servers from a config file naming a command and its
arguments, so `veloce mcp run app:app` is what goes there instead of a wrapper
script whose only job is to call `mount_mcp`. `veloce mcp list` is the MCP
counterpart to `veloce routes`: it answers "did my tool register, and under what
name" without launching a client to ask.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from veloce.cli import build_parser, main

_APP_SOURCE = """
from veloce import Veloce

app = Veloce(title="Inventory", version="1.0.0", openapi_url=None)


@app.mcp_tool(description="Count the units on hand")
async def stock_level(part: str) -> dict:
    return {"part": part, "units": 42}


@app.get(
    "/parts/{part}",
    expose_as_mcp_resource=True,
    mcp_resource_uri="part://{part}",
    mcp_description="One part record",
)
async def part(part: str) -> dict:
    return {"part": part}


@app.mcp_prompt(description="Draft a restock request")
async def restock_note(part: str) -> str:
    return f"Please restock {part}."
"""


@pytest.fixture
def mcp_app(tmp_path, monkeypatch) -> str:
    """Write an importable app module and return its reference."""
    (tmp_path / "cli_mcp_app.py").write_text(textwrap.dedent(_APP_SOURCE))
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cli_mcp_app", None)
    return "cli_mcp_app:app"


# ── The parser ───────────────────────────────────────────────────────


def test_run_defaults_to_stdio():
    """stdio is what an MCP client launches, so it needs no flag."""
    args = build_parser().parse_args(["mcp", "run", "demo:app"])
    assert args.command == "mcp"
    assert args.mcp_command == "run"
    assert args.app == "demo:app"
    assert args.transport == "stdio"


def test_run_accepts_the_http_transport_and_its_options():
    args = build_parser().parse_args(
        ["mcp", "run", "demo:app", "--transport", "http", "--path", "/agent", "--port", "9001"]
    )
    assert args.transport == "http"
    assert args.path == "/agent"
    assert args.port == 9001
    assert args.sessions is False


def test_sessions_is_opt_in():
    args = build_parser().parse_args(
        ["mcp", "run", "demo:app", "--transport", "http", "--sessions"]
    )
    assert args.sessions is True


def test_an_unknown_transport_is_refused():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["mcp", "run", "demo:app", "--transport", "carrier-pigeon"])


def test_mcp_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["mcp"])


def test_list_parses():
    args = build_parser().parse_args(["mcp", "list", "demo:app"])
    assert args.mcp_command == "list"
    assert args.app == "demo:app"


# ── Listing what a client would see ──────────────────────────────────


def test_list_prints_each_primitive(mcp_app, capsys):
    assert main(["mcp", "list", mcp_app]) == 0
    out = capsys.readouterr().out
    assert "TOOLS" in out and "stock_level" in out
    assert "RESOURCES" in out and "part://{part}" in out
    assert "PROMPTS" in out and "restock_note" in out


def test_list_prints_the_descriptions_an_agent_reads(mcp_app, capsys):
    main(["mcp", "list", mcp_app])
    assert "Count the units on hand" in capsys.readouterr().out


def test_list_says_so_when_there_is_nothing_to_serve(tmp_path, monkeypatch, capsys):
    (tmp_path / "cli_bare_app.py").write_text(
        "from veloce import Veloce\napp = Veloce(openapi_url=None)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cli_bare_app", None)
    assert main(["mcp", "list", "cli_bare_app:app"]) == 0
    assert "No MCP tools" in capsys.readouterr().out


def test_list_omits_a_section_with_nothing_in_it(tmp_path, monkeypatch, capsys):
    (tmp_path / "cli_tools_only.py").write_text(
        textwrap.dedent(
            """
            from veloce import Veloce
            app = Veloce(openapi_url=None)

            @app.mcp_tool(description="Only a tool")
            async def solo() -> int:
                return 1
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cli_tools_only", None)
    main(["mcp", "list", "cli_tools_only:app"])
    out = capsys.readouterr().out
    assert "TOOLS" in out
    assert "RESOURCES" not in out
    assert "PROMPTS" not in out


def test_a_target_that_is_not_an_app_is_refused(tmp_path, monkeypatch):
    (tmp_path / "cli_not_an_app.py").write_text("app = object()\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cli_not_an_app", None)
    with pytest.raises(SystemExit, match="mount_mcp"):
        main(["mcp", "list", "cli_not_an_app:app"])


# ── Serving ──────────────────────────────────────────────────────────


def _serves_mcp_at(app, path: str) -> bool:
    """Whether `app` answers a JSON-RPC POST at `path`.

    The transport registers its endpoint `include_in_schema=False`, so it is
    deliberately absent from `app.routes`; asking it to serve is the check.
    """
    from veloce import TestClient

    response = TestClient(app).post(
        path,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"accept": "application/json", "content-type": "application/json"},
    )
    return response.status_code == 200 and "tools" in response.json().get("result", {})


def test_run_serves_stdio_by_handing_the_coroutine_to_asyncio(mcp_app, monkeypatch):
    """stdio is the launched form: the serve coroutine runs to completion."""
    served: dict = {}

    def fake_run(coro) -> None:
        served["awaited"] = coro.__qualname__
        coro.close()

    monkeypatch.setattr("veloce.cli.asyncio.run", fake_run)
    assert main(["mcp", "run", mcp_app]) == 0
    # Named by the mixin that defines it, like every other `Veloce` method.
    assert "mount_mcp" in served["awaited"]


def test_run_mounts_the_http_transport_and_serves_that_app(mcp_app, monkeypatch):
    """The mounted instance is served, not a freshly imported one."""
    served: dict = {}

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs) -> None:
            served["app"] = app
            served["kwargs"] = kwargs

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    assert main(["mcp", "run", mcp_app, "--transport", "http", "--port", "9100"]) == 0

    assert served["kwargs"]["port"] == 9100
    assert served["kwargs"]["host"] == "127.0.0.1"
    # The endpoint answers on the very object handed to the server, rather than on
    # a freshly imported copy that never had the transport mounted.
    assert _serves_mcp_at(served["app"], "/mcp")


def test_run_mounts_at_the_requested_path(mcp_app, monkeypatch):
    served: dict = {}

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs) -> None:
            served["app"] = app

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    main(["mcp", "run", mcp_app, "--transport", "http", "--path", "/agent"])
    assert _serves_mcp_at(served["app"], "/agent")
    assert not _serves_mcp_at(served["app"], "/mcp")


def test_run_falls_back_to_the_built_in_server_without_uvicorn(mcp_app, monkeypatch):
    served: dict = {}

    real_import = __import__

    def no_uvicorn(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("no uvicorn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setitem(sys.modules, "uvicorn", None)
    monkeypatch.delitem(sys.modules, "uvicorn")
    monkeypatch.setattr("builtins.__import__", no_uvicorn)
    monkeypatch.setattr(
        "veloce.Veloce.run", lambda self, **kwargs: served.update(kwargs), raising=False
    )
    assert main(["mcp", "run", mcp_app, "--transport", "http", "--port", "9200"]) == 0
    assert served["port"] == 9200


def test_all_interfaces_becomes_bind_all_on_the_built_in_server(mcp_app, monkeypatch):
    """The native server takes `bind_all=True`, not an all-interfaces host."""
    served: dict = {}

    real_import = __import__

    def no_uvicorn(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("no uvicorn")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "uvicorn", raising=False)
    monkeypatch.setattr("builtins.__import__", no_uvicorn)
    monkeypatch.setattr(
        "veloce.Veloce.run", lambda self, **kwargs: served.update(kwargs), raising=False
    )
    main(["mcp", "run", mcp_app, "--transport", "http", "--host", "0.0.0.0"])
    assert served["bind_all"] is True
    assert "host" not in served
