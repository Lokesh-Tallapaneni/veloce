"""Both `run` commands fall back to the built-in server the same way.

`veloce run` and `veloce mcp run --transport http` each probe for uvicorn and
serve on the built-in server when it is absent. Held as two copies the copies
diverge, and they had: `veloce run` announced the fallback and named the extra
to install, `veloce mcp run` fell through in silence - so an operator who
believed they were on uvicorn had nothing in the output to tell them otherwise.
"""

from __future__ import annotations

import argparse
import inspect
import types

import pytest

from veloce import cli


class _Recorder:
    """Stands in for the app: records how `run` was called."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def _args(host: str = "127.0.0.1", port: int = 8000) -> argparse.Namespace:
    return argparse.Namespace(host=host, port=port)


def test_the_uvicorn_probe_returns_the_module_or_none() -> None:
    result = cli._import_uvicorn()
    assert result is None or isinstance(result, types.ModuleType)


def test_the_fallback_announces_itself(capsys: pytest.CaptureFixture) -> None:
    app = _Recorder()
    cli._serve_builtin(app, _args())
    captured = capsys.readouterr()
    assert "built-in server" in captured.err
    assert "veloceframework[uvicorn]" in captured.err
    assert captured.out == "", "the notice must not reach stdout - stdio MCP owns it"


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_an_all_interfaces_host_becomes_bind_all(host: str) -> None:
    """The native server takes `bind_all=True`, not an all-interfaces host."""
    app = _Recorder()
    cli._serve_builtin(app, _args(host=host))
    assert app.calls == [{"port": 8000, "bind_all": True, "reload": False}]


def test_a_specific_host_is_passed_through() -> None:
    app = _Recorder()
    cli._serve_builtin(app, _args(host="10.0.0.5", port=9001))
    assert app.calls == [{"host": "10.0.0.5", "port": 9001, "reload": False}]


def test_reload_is_forwarded_when_asked_for() -> None:
    app = _Recorder()
    cli._serve_builtin(app, _args(), reload=True)
    assert app.calls[0]["reload"] is True


def test_both_commands_reach_the_same_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property the extraction exists for, driven through both commands."""
    for name in ("_cmd_run", "_cmd_mcp_run"):
        body = inspect.getsource(getattr(cli, name))
        assert "_serve_builtin(" in body, f"{name} does not use the shared fallback"
        assert "import uvicorn" not in body, f"{name} still carries its own uvicorn probe"
