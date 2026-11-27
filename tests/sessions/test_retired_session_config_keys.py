"""A config key that no longer configures anything must not be silent.

Seven keys used to configure the session cookie. When the constructor became
their only source, four were registered so that setting one stops the boot with
a message naming it — and three were not. Those three stayed in
`default_config()`, stayed documented, and did nothing.

That is the worst shape a setting can have: `PERMANENT_SESSION_LIFETIME=3600`
looked applied, read back correctly from `app.config`, and kept the 31-day
default on the wire.
"""

from __future__ import annotations

import inspect

import pytest

from veloce import SecurityHeadersMiddleware, ServerSessionMiddleware, SessionMiddleware, Veloce
from veloce.audit import AuditFailed, run
from veloce.config import Config
from veloce.middleware.sessions import _RETIRED_CONFIG_KEYS
from veloce.testclient import TestClient

RETIRED = [
    "APPLICATION_ROOT",
    "MAX_COOKIE_SIZE",
    "PERMANENT_SESSION_LIFETIME",
    "SESSION_COOKIE_HTTPONLY",
    "SESSION_COOKIE_NAME",
    "SESSION_COOKIE_SAMESITE",
    "SESSION_COOKIE_SECURE",
]


def _hardened() -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(
        SecurityHeadersMiddleware(
            hsts_max_age=31536000, content_security_policy="default-src 'self'"
        )
    )
    return app


# ── none of the seven is seeded any more ─────────────────────────────


@pytest.mark.parametrize("key", RETIRED)
def test_a_retired_key_is_not_a_default(key):
    """Seeding it would advertise a knob that does not turn."""
    assert key not in Config.default_config()
    assert key not in Veloce(openapi_url=None).config


def test_all_seven_are_registered_as_retired():
    assert sorted(_RETIRED_CONFIG_KEYS) == RETIRED


# ── setting one stops the boot ───────────────────────────────────────


@pytest.mark.parametrize("key", RETIRED)
def test_setting_a_retired_key_refuses_the_boot(key):
    """The three that were unregistered did nothing and said nothing."""
    app = _hardened()
    app.config[key] = "anything"
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, secure=True))
    with pytest.raises(AuditFailed) as exc:
        TestClient(app)
    assert exc.value.findings[0].id == "session-config-retired"
    assert key in str(exc.value)


@pytest.mark.parametrize(
    ("key", "argument"),
    [
        ("APPLICATION_ROOT", "path="),
        ("MAX_COOKIE_SIZE", "max_cookie_size="),
        ("PERMANENT_SESSION_LIFETIME", "permanent_lifetime="),
        ("SESSION_COOKIE_SECURE", "secure="),
    ],
)
def test_the_message_names_the_argument_that_replaces_the_key(key, argument):
    """A finding that does not say what to write instead is half a finding."""
    app = _hardened()
    app.config[key] = "anything"
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, secure=True))
    finding = next(f for f in run(app) if f.id == "session-config-retired")
    assert argument in str(finding)


def test_several_retired_keys_at_once_all_get_named():
    app = _hardened()
    app.config["APPLICATION_ROOT"] = "/app"
    app.config["MAX_COOKIE_SIZE"] = 2048
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, secure=True))
    finding = next(f for f in run(app) if f.id == "session-config-retired")
    assert "APPLICATION_ROOT" in str(finding)
    assert "MAX_COOKIE_SIZE" in str(finding)
    assert "path=" in str(finding) and "max_cookie_size=" in str(finding)


def test_the_server_side_backend_reports_it_too():
    app = _hardened()
    app.config["PERMANENT_SESSION_LIFETIME"] = 60
    app.add_middleware(ServerSessionMiddleware(secure=True))
    finding = next(f for f in run(app) if f.id == "session-config-retired")
    assert "ServerSessionMiddleware" in str(finding)


# ── the settings still work, through the constructor ─────────────────


def test_the_lifetime_is_honoured_from_the_constructor():
    middleware = SessionMiddleware(secret_key="k" * 32, permanent_lifetime=60)
    assert middleware.permanent_lifetime == 60


def test_the_cookie_path_is_honoured_from_the_constructor():
    middleware = SessionMiddleware(secret_key="k" * 32, path="/app")
    assert middleware.path == "/app"


def test_the_cookie_size_ceiling_is_honoured_from_the_constructor():
    middleware = SessionMiddleware(secret_key="k" * 32, max_cookie_size=2048)
    assert middleware.max_cookie_size == 2048


def test_the_cookie_path_reaches_the_wire():
    """End to end, not just the attribute."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, path="/app"))

    @app.get("/set")
    async def set_value(request):
        request.session["n"] = 1
        return {}

    assert "Path=/app" in TestClient(app).get("/set").headers["set-cookie"]


# ── edges ────────────────────────────────────────────────────────────


def test_an_app_that_sets_none_of_them_boots_clean():
    app = _hardened()
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, secure=True))
    assert run(app) == []
    assert TestClient(app).get("/nope").status_code == 404


def test_a_retired_key_set_to_none_is_not_reported():
    """`None` is indistinguishable from unset, so it must not fire."""
    app = _hardened()
    app.config["MAX_COOKIE_SIZE"] = None
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, secure=True))
    assert run(app) == []


def test_a_retired_key_without_a_session_middleware_is_not_reported():
    """Nothing reads it, so nothing has standing to complain about it."""
    app = _hardened()
    app.config["PERMANENT_SESSION_LIFETIME"] = 60
    assert [f.id for f in run(app)] == []


def test_a_retired_key_can_be_silenced_like_any_finding():
    app = _hardened()
    app.config["MAX_COOKIE_SIZE"] = 2048
    app.config["SILENCED_AUDIT_IDS"] = ("session-config-retired",)
    app.add_middleware(SessionMiddleware(secret_key="k" * 32, secure=True))
    assert run(app) == []


def test_the_retired_table_names_no_argument_the_constructor_lacks():
    """A hint pointing at a keyword that does not exist is worse than none."""
    accepted = set(inspect.signature(SessionMiddleware.__init__).parameters)
    missing = [arg for arg in _RETIRED_CONFIG_KEYS.values() if arg not in accepted]
    assert missing == [], f"retired keys pointing at unknown arguments: {missing}"
