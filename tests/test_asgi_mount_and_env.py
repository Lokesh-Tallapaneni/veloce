"""F9 — `app.mount()` for arbitrary ASGI apps, and `.env` config loading.

`mount()` now accepts any ASGI application, not just a veloce sub-app:
the prefix is moved from the scope's `path` to `root_path` and the app
is dispatched at the ASGI layer. `Config.from_env_file` loads a
dotenv-style file.
"""

from __future__ import annotations

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
