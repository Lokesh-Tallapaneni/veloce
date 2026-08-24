"""Five places that named a class, a module or a literal instead of asking.

Each is the same shape: a decision made by identity rather than by capability,
so anything the framework did not ship could not participate — and in one case
the framework disagreed with itself.

- `mount()` gated on `isinstance(app, StaticFiles)` while the dispatcher needs
  only `.prefix` and `handle(request)`.
- `run(access_log=True)` recognised an existing access log by which *module*
  defined it, so a user's own could not suppress the built-in one.
- "What subdomain is this?" was derived twice and the two disagreed on an IP
  literal — the router matched, the handler saw nothing.
- `TestClient` switched off the app's setup lock and never switched it back.
- `StaticFiles` wrote `max-age=3600` as a literal in two places and ignored
  `SEND_FILE_MAX_AGE_DEFAULT`, which `send_file` honours.
"""

from __future__ import annotations

import pytest

from veloce import Response, Veloce
from veloce.contrib.staticfiles import StaticFiles
from veloce.testclient import TestClient

# ── mount() asks for the protocol, not the class ─────────────────────


class MemoryAssets:
    """Exactly what the dispatcher needs: a prefix and an async handle."""

    def __init__(self, files: dict[str, str]) -> None:
        self.prefix = ""
        self.files = files

    async def handle(self, request):
        name = request.path[len(self.prefix) :].lstrip("/")
        if name in self.files:
            return Response(body=self.files[name].encode(), content_type="text/plain")
        return None


def test_a_handler_implementing_the_protocol_can_be_mounted():
    """The defect: rejected for not being a `StaticFiles`."""
    app = Veloce(openapi_url=None)
    app.mount("/assets", MemoryAssets({"a.txt": "hello"}))
    client = TestClient(app)
    assert client.get("/assets/a.txt").text == "hello"


def test_a_mounted_handler_that_returns_none_falls_through():
    app = Veloce(openapi_url=None)
    app.mount("/assets", MemoryAssets({}))

    @app.get("/assets/live")
    async def live():
        return {"route": True}

    client = TestClient(app)
    assert client.get("/assets/live").json() == {"route": True}
    assert client.get("/assets/missing").status_code == 404


def test_the_mount_prefix_is_written_onto_the_handler():
    handler = MemoryAssets({})
    Veloce(openapi_url=None).mount("/assets/", handler)
    assert handler.prefix == "/assets"


def test_a_real_staticfiles_still_mounts(tmp_path):
    (tmp_path / "a.txt").write_text("disk", encoding="utf-8")
    app = Veloce(openapi_url=None)
    app.mount("/s", StaticFiles(directory=str(tmp_path)))
    assert TestClient(app).get("/s/a.txt").text == "disk"


def test_something_that_is_neither_is_still_rejected():
    with pytest.raises(TypeError):
        Veloce(openapi_url=None).mount("/x", object())


# ── the access log asks the hook ─────────────────────────────────────


def test_a_user_access_log_suppresses_the_built_in_one():
    """The defect: identified by `__module__`, so only Veloce's own counted."""
    app = Veloce(openapi_url=None)

    def my_access_log(metrics):
        pass

    my_access_log.is_access_log = True
    app.add_instrumentation(my_access_log)
    app._install_dev_access_log()
    assert app._instrumentation == [my_access_log]


def test_an_unmarked_hook_does_not_suppress_it():
    app = Veloce(openapi_url=None)

    def timing(metrics):
        pass

    app.add_instrumentation(timing)
    app._install_dev_access_log()
    assert len(app._instrumentation) == 2


def test_the_built_in_access_log_is_installed_once():
    app = Veloce(openapi_url=None)
    app._install_dev_access_log()
    app._install_dev_access_log()
    assert len(app._instrumentation) == 1


def test_the_built_in_hook_carries_the_marker():
    """So a second installer can recognise it the same way."""
    app = Veloce(openapi_url=None)
    app._install_dev_access_log()
    assert getattr(app._instrumentation[0], "is_access_log", False) is True


# ── one answer to "what subdomain is this" ───────────────────────────


def test_an_ip_literal_host_matches_no_subdomain_route():
    """The defect: the router matched `192`, the handler saw `''`."""
    app = Veloce(openapi_url=None)

    @app.get("/", subdomain="192")
    async def h(request):
        return {"subdomain": request.subdomain}

    assert TestClient(app).get("/", headers={"Host": "192.168.1.1"}).status_code == 404


def test_a_named_subdomain_still_matches():
    app = Veloce(openapi_url=None)
    app.config["SERVER_NAME"] = "example.com"

    @app.get("/", subdomain="api")
    async def h(request):
        return {"subdomain": request.subdomain}

    client = TestClient(app)
    assert client.get("/", headers={"Host": "api.example.com"}).json() == {"subdomain": "api"}
    assert client.get("/", headers={"Host": "www.example.com"}).status_code == 404


def test_a_wildcard_subdomain_matches_any_non_empty_one():
    app = Veloce(openapi_url=None)
    app.config["SERVER_NAME"] = "example.com"

    @app.get("/", subdomain="*")
    async def h(request):
        return {"subdomain": request.subdomain}

    client = TestClient(app)
    assert client.get("/", headers={"Host": "api.example.com"}).status_code == 200
    assert client.get("/", headers={"Host": "example.com"}).status_code == 404


def test_the_router_and_the_handler_agree():
    """The property: whatever matched is what the handler is told."""
    app = Veloce(openapi_url=None)
    app.config["SERVER_NAME"] = "example.com"

    @app.get("/", subdomain="api")
    async def h(request):
        return {"subdomain": request.subdomain}

    body = TestClient(app).get("/", headers={"Host": "api.example.com"}).json()
    assert body["subdomain"] == "api"


def test_a_wildcard_does_not_match_an_ip_literal():
    app = Veloce(openapi_url=None)

    @app.get("/", subdomain="*")
    async def h(request):
        return {}

    assert TestClient(app).get("/", headers={"Host": "10.0.0.1"}).status_code == 404


# ── the test client hands the app back ───────────────────────────────


def test_the_setup_lock_is_restored_on_close():
    """The defect: an app touched by a client never froze its routes again."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return {}

    assert app._setup_lock_enabled is True
    with TestClient(app) as client:
        client.get("/x")
        assert app._setup_lock_enabled is False
    assert app._setup_lock_enabled is True


def test_an_explicit_close_restores_it_too():
    app = Veloce(openapi_url=None)
    client = TestClient(app)
    assert app._setup_lock_enabled is False
    client.close()
    assert app._setup_lock_enabled is True


def test_closing_twice_is_harmless():
    app = Veloce(openapi_url=None)
    client = TestClient(app)
    client.close()
    client.close()
    assert app._setup_lock_enabled is True


def test_an_app_that_had_it_off_keeps_it_off():
    """Restored to what it was, not to True."""
    app = Veloce(openapi_url=None)
    app._setup_lock_enabled = False
    with TestClient(app):
        pass
    assert app._setup_lock_enabled is False


def test_late_registration_still_works_inside_the_client():
    """The reason the lock is relaxed at all must survive the fix."""
    app = Veloce(openapi_url=None)
    with TestClient(app) as client:

        @app.get("/late")
        async def late():
            return {"ok": True}

        assert client.get("/late").json() == {"ok": True}


# ── one cache lifetime for both ways of serving a file ───────────────


@pytest.fixture
def asset_dir(tmp_path):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    return tmp_path


def _cache_control(asset_dir, *, configured=None, **kwargs) -> str:
    app = Veloce(openapi_url=None)
    if configured is not None:
        app.config["SEND_FILE_MAX_AGE_DEFAULT"] = configured
    app.mount("/s", StaticFiles(directory=str(asset_dir), **kwargs))
    return TestClient(app).get("/s/a.txt").headers["cache-control"]


def test_the_app_wide_default_reaches_static_files(asset_dir):
    """The defect: `send_file` honoured it and this handler did not."""
    assert _cache_control(asset_dir, configured=60) == "public, max-age=60"


def test_an_explicit_handler_max_age_wins(asset_dir):
    assert _cache_control(asset_dir, configured=60, max_age=99) == "public, max-age=99"


def test_neither_set_keeps_the_previous_default(asset_dir):
    """No deployment changes by upgrading."""
    assert _cache_control(asset_dir) == "public, max-age=3600"


def test_a_zero_max_age_is_honoured_not_treated_as_unset(asset_dir):
    assert _cache_control(asset_dir, max_age=0) == "public, max-age=0"


def test_a_range_request_uses_the_same_lifetime(asset_dir):
    """The literal was written twice; both had to move."""
    app = Veloce(openapi_url=None)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60
    app.mount("/s", StaticFiles(directory=str(asset_dir)))
    response = TestClient(app).get("/s/a.txt", headers={"Range": "bytes=0-0"})
    assert response.status_code == 206
    assert response.headers["cache-control"] == "public, max-age=60"
