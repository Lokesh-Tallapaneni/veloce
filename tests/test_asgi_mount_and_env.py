"""F9 — `app.mount()` for arbitrary ASGI apps, and `.env` config loading.

`mount()` now accepts any ASGI application, not just a veloce sub-app:
the prefix is moved from the scope's `path` to `root_path` and the app
is dispatched at the ASGI layer. `Config.from_env_file` loads a
dotenv-style file.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce import Request, Veloce


async def _tiny_asgi(scope, receive, send):
    """A minimal standalone ASGI app — echoes the scope it was handed."""
    path = scope["path"]
    root = scope.get("root_path", "")
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": f"path={path} root={root}".encode()})


# ── mounting an arbitrary ASGI app ────────────────────────────────────


def test_mounted_asgi_app_handles_requests_under_its_prefix():
    app = Veloce(debug=True, openapi_url=None)
    app.mount("/ext", _tiny_asgi)

    resp = app.test_client().get("/ext/hello")
    assert resp.status_code == 200
    # The prefix moved from `path` to `root_path`.
    assert resp.body == b"path=/hello root=/ext"


def test_mounted_asgi_app_at_exact_prefix():
    app = Veloce(debug=True, openapi_url=None)
    app.mount("/ext", _tiny_asgi)

    resp = app.test_client().get("/ext")
    assert resp.status_code == 200
    assert resp.body == b"path=/ root=/ext"


def test_mounted_asgi_app_does_not_shadow_other_routes():
    app = Veloce(debug=True, openapi_url=None)
    app.mount("/ext", _tiny_asgi)

    @app.get("/native")
    async def native():
        return {"handler": "veloce"}

    client = app.test_client()
    assert client.get("/ext/x").body == b"path=/x root=/ext"
    assert client.get("/native").json() == {"handler": "veloce"}


def test_mounted_asgi_app_passes_response_headers_through():
    app = Veloce(debug=True, openapi_url=None)
    app.mount("/ext", _tiny_asgi)

    resp = app.test_client().get("/ext/y")
    assert resp.headers.get("content-type") == "text/plain"


def test_unmatched_path_is_not_routed_to_a_mount():
    app = Veloce(debug=True, openapi_url=None)
    app.mount("/ext", _tiny_asgi)

    # `/external` must not match the `/ext` prefix.
    assert app.test_client().get("/external").status_code == 404


def test_veloce_sub_app_still_uses_the_native_mount_path():
    app = Veloce(debug=True, openapi_url=None)
    sub = Veloce(debug=True, openapi_url=None)

    @sub.get("/ping")
    async def ping(request: Request):
        return {"sub": "pong"}

    app.mount("/sub", sub)
    # A veloce sub-app is recognised as native, not an ASGI mount.
    assert app._asgi_mounts == []
    assert len(app._mounted_apps) == 1

    assert app.test_client().get("/sub/ping").json() == {"sub": "pong"}


# ── .env config loading ───────────────────────────────────────────────


def test_from_env_file_loads_key_value_pairs(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "SECRET_KEY=s3cr3t\n"
        "export DATABASE_URL=postgres://localhost/db\n"
        'QUOTED="quoted value"\n'
        "SINGLE='single quoted'\n"
        "lowercase=ignored\n"
    )
    app = Veloce(openapi_url=None)
    loaded = app.config.from_env_file(str(env))

    assert loaded is True
    assert app.config["SECRET_KEY"] == "s3cr3t"
    assert app.config["DATABASE_URL"] == "postgres://localhost/db"
    assert app.config["QUOTED"] == "quoted value"
    assert app.config["SINGLE"] == "single quoted"
    # Only UPPERCASE keys are stored.
    assert "lowercase" not in app.config


def test_from_env_file_missing_file_silent_returns_false(tmp_path):
    app = Veloce(openapi_url=None)
    assert app.config.from_env_file(str(tmp_path / "absent.env"), silent=True) is False


def test_from_env_file_missing_file_raises_without_silent(tmp_path):
    app = Veloce(openapi_url=None)
    with pytest.raises(OSError):
        app.config.from_env_file(str(tmp_path / "absent.env"))


def test_from_env_file_ignores_malformed_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("VALID=ok\nthis line has no equals sign\nANOTHER=fine\n")
    app = Veloce(openapi_url=None)
    app.config.from_env_file(str(env))

    assert app.config["VALID"] == "ok"
    assert app.config["ANOTHER"] == "fine"


def test_from_env_file_strips_an_unquoted_inline_comment(tmp_path):
    """An unquoted value drops a trailing ` #` inline comment instead of
    keeping it as part of the value."""
    env = tmp_path / ".env"
    env.write_text("HOST=localhost  # the dev host\nPORT=8000\n")
    app = Veloce(openapi_url=None)
    app.config.from_env_file(str(env))

    assert app.config["HOST"] == "localhost"
    assert app.config["PORT"] == "8000"


def test_from_env_file_keeps_a_hash_inside_a_quoted_value(tmp_path):
    """A `#` inside quotes is literal; only a comment after the closing
    quote is stripped."""
    env = tmp_path / ".env"
    env.write_text('PASSWORD="p#ss w#rd"  # not part of the value\n')
    app = Veloce(openapi_url=None)
    app.config.from_env_file(str(env))

    assert app.config["PASSWORD"] == "p#ss w#rd"


def test_from_env_file_keeps_a_bare_hash_without_leading_space(tmp_path):
    """Only a whitespace-delimited ` #` starts a comment — a `#` with no
    leading space is part of the value."""
    env = tmp_path / ".env"
    env.write_text("COLOR=#ff0000\n")
    app = Veloce(openapi_url=None)
    app.config.from_env_file(str(env))

    assert app.config["COLOR"] == "#ff0000"


# ── mount edge cases ──────────────────────────────────────────────────


async def _tiny_ws_asgi(scope, receive, send):
    """A minimal standalone ASGI WebSocket app — echoes its scope path."""
    await receive()  # websocket.connect
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.send", "text": f"ws path={scope['path']}"})
    await send({"type": "websocket.close"})


def test_mounted_asgi_app_receives_websocket_scopes():
    app = Veloce(debug=True, openapi_url=None)
    app.mount("/wsext", _tiny_ws_asgi)

    client = app.test_client()
    with client.websocket_connect("/wsext/room") as conn:
        # The mounted app saw the prefix-stripped path.
        assert conn.receive_text() == "ws path=/room"


def test_mount_normalises_a_missing_leading_slash():
    app = Veloce(debug=True, openapi_url=None)
    app.mount("ext", _tiny_asgi)  # no leading slash

    resp = app.test_client().get("/ext/x")
    assert resp.status_code == 200
    assert resp.body == b"path=/x root=/ext"


def test_a_prefix_nested_under_an_existing_mount_is_rejected():
    """Overlapping mounts would shadow each other order-dependently, so a
    prefix nested under an existing mount is rejected outright."""
    app = Veloce(debug=True, openapi_url=None)
    app.mount("/api", _tiny_asgi)
    with pytest.raises(ValueError, match="overlaps"):
        app.mount("/api/v2", _tiny_asgi)


def test_a_prefix_containing_an_existing_mount_is_rejected():
    app = Veloce(debug=True, openapi_url=None)
    app.mount("/api/v2", _tiny_asgi)
    with pytest.raises(ValueError, match="overlaps"):
        app.mount("/api", _tiny_asgi)  # would contain the existing /api/v2


def test_a_duplicate_mount_prefix_is_rejected():
    app = Veloce(debug=True, openapi_url=None)
    app.mount("/api", _tiny_asgi)
    with pytest.raises(ValueError, match="overlaps"):
        app.mount("/api", _tiny_asgi)


def test_sibling_mount_prefixes_are_allowed():
    """Non-overlapping prefixes register fine — including one that shares
    leading text but is not a path-segment ancestor."""
    app = Veloce(debug=True, openapi_url=None)
    app.mount("/api", _tiny_asgi)
    app.mount("/apix", _tiny_asgi)  # shares text with /api, not an ancestor
    app.mount("/web", _tiny_asgi)
    assert len(app._asgi_mounts) == 3


def test_overlap_is_detected_across_asgi_and_veloce_mounts():
    """An ASGI mount and a veloce sub-app are tracked in separate lists,
    but a prefix overlap between the two is still rejected."""
    app = Veloce(debug=True, openapi_url=None)
    sub = Veloce(debug=True, openapi_url=None)
    app.mount("/svc", sub)  # veloce sub-app
    with pytest.raises(ValueError, match="overlaps"):
        app.mount("/svc/inner", _tiny_asgi)  # ASGI app nested under it


def test_mounted_asgi_app_does_not_receive_lifespan_scopes():
    """The parent owns the lifespan cycle — a mounted ASGI app sees only
    http / websocket scopes and must self-initialise."""
    seen: list[str] = []

    async def recorder(scope, receive, send):
        seen.append(scope["type"])
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    app = Veloce(debug=True, openapi_url=None)
    app.mount("/ext", recorder)

    async def drive_lifespan():
        events = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

        async def receive():
            return events.pop(0)

        async def send(message):
            pass

        await app({"type": "lifespan"}, receive, send)

    asyncio.run(drive_lifespan())
    assert "lifespan" not in seen  # never forwarded to the mount

    app.test_client().get("/ext/x")
    assert "http" in seen  # http still reaches it
