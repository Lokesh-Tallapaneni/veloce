"""The startup banner names the Veloce version, not the app's API version.

`app.run()` printed `f"Veloce v{self.version}"`, and `self.version` is the
constructor's `version=` argument - documented as "API version string emitted
into the OpenAPI document", defaulting to `"0.1.0"`. So a default app running
Veloce 0.17.1 printed:

    Veloce v0.1.0
    Listening on http://127.0.0.1:8731

Two different numbers, one of them wrong, on the line an operator reads to
establish which framework version is deployed - which is what that line is read
for during an incident.

The banner now resolves the installed distribution version, the same source
`veloce.__version__` and `veloce --version` use, so all three agree.

The three lines moved into `_print_banner` to be testable at all: they were
inside `run()`, after the event-loop policy is set and before the loop starts,
so nothing could reach them without binding a socket. The extraction is
behaviour-preserving - same text, same order, same `access_log` gate.
"""

from __future__ import annotations

import veloce
from veloce import Veloce
from veloce._version import UNKNOWN_VERSION, resolve_version


def _banner(app: Veloce, capsys, **kwargs) -> str:
    app._print_banner(kwargs.pop("host", "127.0.0.1"), kwargs.pop("port", 8000), **kwargs)
    return capsys.readouterr().out


# ── the version it reports ───────────────────────────────────────────


def test_the_banner_reports_the_framework_version(capsys):
    """The defect: it reported the app's OpenAPI version instead."""
    app = Veloce(version="9.9.9", openapi_url=None)
    output = _banner(app, capsys)
    assert f"Veloce v{resolve_version()}" in output


def test_the_banner_does_not_report_the_apps_api_version(capsys):
    """The exact confusion: an app declaring 9.9.9 must not print `Veloce v9.9.9`."""
    app = Veloce(version="9.9.9", openapi_url=None)
    assert "Veloce v9.9.9" not in _banner(app, capsys)


def test_a_default_app_does_not_print_the_default_openapi_version(capsys):
    """`version=` defaults to 0.1.0, which is what made this so easy to miss."""
    app = Veloce(openapi_url=None)
    assert app.version == "0.1.0"
    assert "Veloce v0.1.0" not in _banner(app, capsys)


def test_the_banner_agrees_with_the_dunder_version(capsys):
    """Three places report a version; they must not disagree."""
    app = Veloce(openapi_url=None)
    assert f"Veloce v{veloce.__version__}" in _banner(app, capsys)


def test_the_apps_api_version_is_untouched(capsys):
    """The negative direction: `version=` still means what it documented."""
    app = Veloce(version="2.5.0", openapi_url=None)
    _banner(app, capsys)
    assert app.version == "2.5.0"


# ── the rest of the banner is unchanged ──────────────────────────────


def test_the_banner_reports_the_listen_address(capsys):
    output = _banner(Veloce(openapi_url=None), capsys, host="0.0.0.0", port=9001)
    assert "Listening on http://0.0.0.0:9001" in output


def test_the_banner_reports_https_when_a_context_is_given(capsys):
    output = _banner(Veloce(openapi_url=None), capsys, host="example.com", port=443, tls=True)
    assert "Listening on https://example.com:443" in output


def test_the_banner_tells_the_reader_how_to_stop(capsys):
    assert "Press Ctrl+C to stop" in _banner(Veloce(openapi_url=None), capsys)


def test_the_banner_is_three_lines(capsys):
    """Pins the shape, so the extraction cannot have dropped or added one."""
    output = _banner(Veloce(openapi_url=None), capsys)
    assert len([line for line in output.splitlines() if line.strip()]) == 3


# ── the resolved version itself ──────────────────────────────────────


def test_the_resolved_version_is_not_the_unknown_sentinel():
    """In a working install the banner must show a real number."""

    assert resolve_version() != UNKNOWN_VERSION


def test_the_resolved_version_matches_the_installed_distribution():
    import importlib.metadata as metadata

    assert resolve_version() == metadata.version("veloceframework")
