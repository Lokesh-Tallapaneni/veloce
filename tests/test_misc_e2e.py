"""End-to-end tests for helpers, sessions, cli, openapi, testclient fixes.

Covers #30, #37, #38 (helpers + json_provider), #48 (session sweep), #52 (env
warning), #54 (cli --version), #46 (openapi dict schema), #55 (absolute
redirect).
"""

from __future__ import annotations

import time

import pytest

import veloce
from veloce import Request, Response, Veloce
from veloce.cli import build_parser
from veloce.helpers import current_app, get_flashed_messages, jsonify
from veloce.sessions import InMemorySessionStore
from veloce.testclient import TestClient


def test_jsonify_via_testclient():
    app = Veloce(openapi_url=None)

    @app.get("/j")
    async def j(request: Request):
        return jsonify({"x": 1})

    with TestClient(app) as client:
        resp = client.get("/j")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"x": 1}


def test_current_app_proxy_resolves_in_request_context():
    app = Veloce(openapi_url=None)
    app.config["SENTINEL"] = "I-resolved"
    observed = {}

    @app.get("/cfg")
    async def cfg_route(request: Request):
        observed["sentinel"] = current_app.config.get("SENTINEL")
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/cfg")
    assert observed["sentinel"] == "I-resolved"


def test_current_app_proxy_outside_request_raises():
    with pytest.raises(RuntimeError):
        _ = current_app.config


def test_inmemory_session_store_sweep_expired_returns_count():
    store = InMemorySessionStore()
    now = time.time()
    store._entries["a"] = ({"x": 1}, now - 100)
    store._entries["b"] = ({"x": 2}, now - 50)
    store._entries["fresh"] = ({"x": 3}, now + 3600)
    removed = store.sweep_expired()
    assert removed == 2
    assert "fresh" in store._entries
    assert "a" not in store._entries
    assert "b" not in store._entries


def test_inmemory_session_store_sweep_tolerates_concurrent_removal():
    class RacingDict(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._racing = True

        def pop(self, key, default=None):
            if self._racing and key == "b":
                self._racing = False
                super().pop("b", None)
            return super().pop(key, default)

    store = InMemorySessionStore()
    now = time.time()
    store._entries = RacingDict({"a": ({}, now - 1), "b": ({}, now - 1), "c": ({}, now - 1)})
    removed = store.sweep_expired()
    assert removed == 2
    assert len(store._entries) == 0


def test_config_from_env_file_warning_on_unmatched_quote(tmp_path, caplog):
    env = tmp_path / ".env"
    env.write_text('FIRST=ok\nDB_URL="postgres://x@y/z\n')
    app = Veloce(openapi_url=None)
    with caplog.at_level("WARNING", logger="veloce.config"):
        app.config.from_env_file(str(env))
    assert app.config["FIRST"] == "ok"
    assert app.config["DB_URL"] == "postgres://x@y/z"
    msgs = [r for r in caplog.records if r.name == "veloce.config"]
    assert msgs, "expected a warning on veloce.config"
    msg = msgs[0].getMessage()
    assert "DB_URL" in msg
    assert "line 2" in msg


def test_cli_version_flag_prints_and_exits(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    output = (captured.out + captured.err).strip()
    assert output == f"veloce {veloce.__version__}"


def test_cli_version_short_flag(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["-V"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    output = (captured.out + captured.err).strip()
    assert output == f"veloce {veloce.__version__}"


def test_openapi_dict_schema_emits_additional_properties():
    from typing import Any

    from veloce.contrib.openapi import _python_type_to_schema

    schema_int = _python_type_to_schema(dict[str, int])
    assert schema_int.get("type") == "object"
    assert schema_int.get("additionalProperties") == {"type": "integer"}

    schema_str = _python_type_to_schema(dict[str, str])
    assert schema_str.get("additionalProperties") == {"type": "string"}

    schema_bare = _python_type_to_schema(dict)
    assert schema_bare.get("type") == "object"
    assert "additionalProperties" not in schema_bare

    # `dict[str, Any]` is the canonical "object with any-typed values" —
    # `additionalProperties` must be `{}` (empty schema), not the string
    # default from the scalar fallback.
    schema_any = _python_type_to_schema(dict[str, Any])
    assert schema_any == {"type": "object", "additionalProperties": {}}


def test_testclient_absolute_redirect_same_host_follows():
    app = Veloce(openapi_url=None)

    @app.get("/from")
    async def src(request: Request):
        return Response(
            status_code=302,
            body=b"",
            headers={"Location": "http://testserver/to?x=1"},
        )

    @app.get("/to")
    async def dst(request: Request):
        return {"got": request.query_params.get("x")}

    with TestClient(app) as client:
        resp = client.get("/from", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.json() == {"got": "1"}


def test_testclient_absolute_redirect_cross_host_raises():
    app = Veloce(openapi_url=None)

    @app.get("/from")
    async def src(request: Request):
        return Response(
            status_code=302,
            body=b"",
            headers={"Location": "http://other-host/somewhere"},
        )

    with TestClient(app) as client, pytest.raises(RuntimeError, match="other-host"):
        client.get("/from", follow_redirects=True)


def test_get_flashed_messages_with_category_filter_set():
    from veloce.helpers import flash
    from veloce.middleware.sessions import SessionMiddleware

    app = Veloce(openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="test-secret-key-32-bytes-long-ok"))
    observed = {}

    @app.get("/show")
    async def show(request: Request):
        flash("hello", "info")
        flash("be careful", "warn")
        flash("noise", "debug")
        msgs = get_flashed_messages(with_categories=True, category_filter=["info", "warn"])
        observed["msgs"] = list(msgs)
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/show")

    msgs = observed.get("msgs", [])
    categories = [c for c, _ in msgs]
    assert "info" in categories
    assert "warn" in categories
    assert "debug" not in categories
