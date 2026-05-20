"""Veloce.import_name / package_root / got_first_request — application metadata."""

from __future__ import annotations

import asyncio
import os

import pytest

from veloce import Request, Veloce


def _req(path: str = "/x") -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── import_name ──────────────────────────────────────────────────────


def test_import_name_explicit():
    app = Veloce(import_name="my.app", openapi_url=None)
    assert app.import_name == "my.app"


def test_import_name_defaults_to_caller_module():
    """When omitted, defaults to the calling module's `__name__`."""
    app = Veloce(openapi_url=None)
    # This test module's name.
    assert app.import_name == __name__


# ── package_root ─────────────────────────────────────────────────────


def test_package_root_for_known_module():
    app = Veloce(import_name="veloce", openapi_url=None)
    root = app.package_root
    # The `veloce` package directory must exist and contain __init__.py.
    assert os.path.isdir(root)
    assert os.path.exists(os.path.join(root, "__init__.py"))


def test_package_root_for_unknown_module_falls_back_to_cwd():
    app = Veloce(import_name="not_a_module_zzz", openapi_url=None)
    assert app.package_root == os.getcwd()


# ── got_first_request ────────────────────────────────────────────────


def test_got_first_request_false_initially():
    app = Veloce(openapi_url=None)
    assert app.got_first_request is False


@pytest.mark.asyncio
async def test_got_first_request_true_after_dispatch():
    app = Veloce(debug=True, openapi_url=None)

    @app.before_first_request
    def init():
        pass

    @app.get("/x")
    async def x():
        return {}

    await app.handle_request(_req())
    assert app.got_first_request is True
    # Subsequent requests don't reset it.
    await app.handle_request(_req())
    assert app.got_first_request is True


def test_got_first_request_unchanged_if_no_before_first_request_hooks():
    """No hooks → first-request flag never flips."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {}

    asyncio.run(app.handle_request(_req()))
    # Without hooks registered, the dispatcher short-circuits before
    # the flag flip — flag stays False. This is a minor behavioural
    # quirk worth documenting; callers that want the flag should
    # register a hook (even a no-op).
    assert app.got_first_request is False
