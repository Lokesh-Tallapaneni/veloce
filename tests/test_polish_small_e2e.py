"""End-to-end coverage for the polish-wave-2 small fixes."""

from __future__ import annotations

import logging
import sys
import time

import pytest

from veloce import Veloce
from veloce.http.response import RedirectResponse
from veloce.middleware.cors import CORSMiddleware
from veloce.middleware.csrf import CSRFMiddleware
from veloce.middleware.logging import LoggingMiddleware
from veloce.safe import safe_join
from veloce.sessions import InMemorySessionStore
from veloce.signals import SignalResult
from veloce.testclient import TestClient

# ── #2: safe_join root edge ─────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX root semantics")
def test_safe_join_root_directory_allows_children() -> None:
    assert safe_join("/", "etc") == "/etc"
    assert safe_join("/", "etc", "passwd") == "/etc/passwd"


# ── #11: csrf `_matches` via header path ────────────────────────────


def test_csrf_header_path_accepts_matching_token() -> None:
    app = Veloce()
    app.add_middleware(CSRFMiddleware(cookie_secure=False))

    @app.get("/seed")
    async def seed() -> dict:
        return {"ok": True}

    @app.post("/submit")
    async def submit() -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/seed")
        token = client.cookies.get("csrf_token")
        assert token, "CSRF cookie should have been minted"
        resp = client.post("/submit", headers={"X-CSRF-Token": token})
        assert resp.status_code == 200, resp.body
        assert resp.json() == {"ok": True}


# ── #23: logging level / handlers gated independently ──────────────


def test_logging_middleware_sets_level_when_handler_preconfigured() -> None:
    logger = logging.getLogger("veloce.access")
    # Snapshot state so we can restore after the test.
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    try:
        logger.handlers = [logging.NullHandler()]
        logger.setLevel(logging.NOTSET)
        assert logger.level == logging.NOTSET

        LoggingMiddleware()

        assert logger.level == logging.INFO
        # The pre-existing NullHandler must not have been duplicated.
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.NullHandler)
    finally:
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)


# ── #43: SignalResult alias ─────────────────────────────────────────


def test_signal_result_is_alias_for_list_of_tuples() -> None:
    # `SignalResult = list[tuple[Callable, Any]]` resolves to a
    # `types.GenericAlias` at runtime — inspect the origin / args.
    origin = getattr(SignalResult, "__origin__", None)
    assert origin is list
    (inner,) = SignalResult.__args__
    assert getattr(inner, "__origin__", None) is tuple
    callable_arg, any_arg = inner.__args__
    # `Callable` from collections.abc is what the type alias resolves to.
    from collections.abc import Callable
    from typing import Any

    assert callable_arg is Callable
    assert any_arg is Any


# ── #48: per-instance sweep config ──────────────────────────────────


async def test_in_memory_session_store_per_instance_sweep_fires() -> None:
    store = InMemorySessionStore(sweep_threshold=2, sweep_probability=1.0)
    # Pre-populate with three already-expired entries by reaching into the
    # internal map — public `write` would refresh the expiry.
    past = time.time() - 60
    store._entries["a"] = ({"x": 1}, past)
    store._entries["b"] = ({"x": 2}, past)
    store._entries["c"] = ({"x": 3}, past)
    assert len(store._entries) == 3

    # The next `write` should trip the per-instance sweep and evict the
    # three expired entries, leaving only the fresh one.
    await store.write("fresh", {"y": 1}, max_age=3600)

    assert set(store._entries) == {"fresh"}


# ── #55: testclient cross-host error includes both hosts ────────────


def test_testclient_cross_host_redirect_error_names_both_hosts() -> None:
    app = Veloce()

    @app.get("/go")
    async def go() -> RedirectResponse:
        return RedirectResponse("http://other-host/x", status_code=302)

    with (
        TestClient(app, follow_redirects=True) as client,
        pytest.raises(RuntimeError) as excinfo,
    ):
        client.get("/go")

    msg = str(excinfo.value)
    assert "other-host" in msg
    assert "testserver" in msg


# ── #58: CORS wildcard regex denylist with credentials ──────────────


@pytest.mark.parametrize("pattern", [".*", ".+", "^.*$"])
def test_cors_wildcard_regex_with_credentials_rejected(pattern: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        CORSMiddleware(allow_credentials=True, allow_origin_regex=pattern)
    assert "allow_credentials" in str(excinfo.value)


# ── #8: greedy-not-final converters route via the regex fallback ────


def test_router_greedy_converter_with_trailing_uses_regex_fallback() -> None:
    # `{p:path}` with a static suffix is no longer rejected at registration;
    # it is handled by the hybrid router's regex fallback.
    app = Veloce()

    @app.get("/files/{p:path}/info/x")
    async def serve(p: str) -> dict:
        return {"p": p}

    match = app.match("GET", "/files/a/b/info/x")
    assert match is not None
    assert match.path_params == {"p": "a/b"}
