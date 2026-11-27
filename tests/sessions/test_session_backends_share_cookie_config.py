"""Both session backends settle their cookie identically.

`SessionMiddleware` and `ServerSessionMiddleware` derive from
`SessionMiddlewareBase` and yet carried the same forty-line settle / validate /
assign block twice: one `_shared_cookie_settings` call, a five-line unpack, a
seven-argument `_validate_cookie_security` call and a dozen identical attribute
assignments.

Two copies of a security check are two things to keep in step, and they had
already drifted in comment and in ordering. The block now lives once, on the
base, as `_configure_cookie`. What each backend keeps is what actually
distinguishes it: the signer and chunking for one, the store for the other.

These tests are the reason that is safe. They pin the *property* the duplication
put at risk - the two backends agree about the cookie - rather than the shape of
either constructor, so they hold whichever way the code is arranged.
"""

from __future__ import annotations

import pytest

from veloce.middleware.sessions import (
    ServerSessionMiddleware,
    SessionMiddleware,
    SessionMiddlewareBase,
)

_COOKIE_ATTRS = (
    "cookie_name",
    "max_age",
    "permanent_lifetime",
    "path",
    "httponly",
    "secure",
    "samesite",
    "domain",
    "cookie_prefix",
    "partitioned",
    "vary_on_cookie",
    "renew_on_access",
    "_wire_cookie_name",
    "_persist_on_status",
)


def _pair(**kwargs):
    """The same cookie settings through both constructors."""
    return (
        SessionMiddleware(secret_key="k" * 32, **kwargs),
        ServerSessionMiddleware(**kwargs),
    )


def _cookie_state(middleware) -> dict:
    return {name: getattr(middleware, name) for name in _COOKIE_ATTRS}


# ── defaults ─────────────────────────────────────────────────────────


def test_the_two_backends_default_identically():
    cookie, server = _pair()
    assert _cookie_state(cookie) == _cookie_state(server)


@pytest.mark.parametrize("attr", _COOKIE_ATTRS)
def test_each_default_attribute_matches(attr):
    """Parameterised so a failure names the setting that drifted."""
    cookie, server = _pair()
    assert getattr(cookie, attr) == getattr(server, attr)


def test_the_defaults_are_not_all_none():
    """A vacuity guard: comparing two objects of `None` would pass anything."""
    state = _cookie_state(SessionMiddleware(secret_key="k" * 32))
    assert state["cookie_name"]
    assert state["max_age"] == 86400 * 14
    assert state["permanent_lifetime"] == 86400 * 31


# ── explicit settings ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "settings",
    [
        {"cookie_name": "sid"},
        {"max_age": 60},
        {"permanent_lifetime": 120},
        {"path": "/app"},
        {"httponly": False},
        {"secure": True},
        {"samesite": "Strict"},
        {"domain": "example.com", "secure": True},
        {"vary_on_cookie": False},
        {"renew_on_access": True},
        {"secure": True, "cookie_prefix": "secure"},
        {"secure": True, "partitioned": True, "samesite": "None"},
    ],
    ids=lambda s: ",".join(s),
)
def test_an_explicit_setting_lands_the_same_way(settings):
    cookie, server = _pair(**settings)
    assert _cookie_state(cookie) == _cookie_state(server)


def test_the_wire_name_takes_the_prefix_in_both():
    """`__Host-`/`__Secure-` must be applied by both, or read and write disagree."""
    cookie, server = _pair(secure=True, cookie_prefix="host", cookie_name="sid")
    assert cookie._wire_cookie_name == server._wire_cookie_name
    assert cookie._wire_cookie_name.startswith("__Host-")


# ── validation fires in both ─────────────────────────────────────────


@pytest.mark.parametrize(
    "settings",
    [
        {"cookie_prefix": "host", "secure": False},
        {"cookie_prefix": "host", "domain": "example.com", "secure": True},
        {"partitioned": True, "secure": False},
    ],
    ids=["host-not-secure", "host-with-domain", "partitioned-not-secure"],
)
def test_an_invalid_combination_is_refused_by_both(settings):
    """The check the duplication put at risk: one copy could have been fixed."""
    with pytest.raises((ValueError, AssertionError)):
        SessionMiddleware(secret_key="k" * 32, **settings)
    with pytest.raises((ValueError, AssertionError)):
        ServerSessionMiddleware(**settings)


def test_a_valid_combination_is_accepted_by_both():
    """The negative: a check that rejected everything would pass the above."""
    assert SessionMiddleware(secret_key="k" * 32, secure=True, cookie_prefix="host")
    assert ServerSessionMiddleware(secure=True, cookie_prefix="host")


# ── and each keeps what makes it itself ──────────────────────────────


def test_only_the_cookie_backend_has_a_signer():
    cookie, server = _pair()
    assert hasattr(cookie, "_signer")
    assert not hasattr(server, "_signer")


def test_only_the_server_backend_has_a_store():
    cookie, server = _pair()
    assert hasattr(server, "store")
    assert not hasattr(cookie, "store")


def test_only_the_cookie_backend_bounds_the_cookie_size():
    cookie, server = _pair()
    assert cookie.max_cookie_size > 0
    assert not hasattr(server, "max_cookie_size")


# ── the shared step is on the base, so a third backend gets it ───────


def test_a_third_backend_can_configure_itself_from_the_base():
    """The point of putting it on the base rather than in a module function."""

    class RedisSessionMiddleware(SessionMiddlewareBase):
        def __init__(self, **kwargs):
            super().__init__()
            self._configure_cookie(
                cookie_name=kwargs.get("cookie_name", "session"),
                max_age=3600,
                permanent_lifetime=2592000,
                path="/",
                httponly=True,
                secure=True,
                samesite="Lax",
                domain=None,
                cookie_prefix=None,
                partitioned=False,
                vary_on_cookie=True,
                persist_on_status=None,
                renew_on_access=False,
            )

    backend = RedisSessionMiddleware()
    assert backend.cookie_name == "session"
    assert backend.secure is True
    assert backend.cookie_is_secure() is True
    assert backend._wire_cookie_name == "session"


def test_a_third_backend_gets_the_validation_too():
    class BadBackend(SessionMiddlewareBase):
        def __init__(self):
            super().__init__()
            self._configure_cookie(
                cookie_name="session",
                max_age=3600,
                permanent_lifetime=2592000,
                path="/",
                httponly=True,
                secure=False,
                samesite="Lax",
                domain=None,
                cookie_prefix="host",
                partitioned=False,
                vary_on_cookie=True,
                persist_on_status=None,
                renew_on_access=False,
            )

    with pytest.raises((ValueError, AssertionError)):
        BadBackend()
