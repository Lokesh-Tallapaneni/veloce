"""What `security_audit` covers, and what it admits it cannot see.

`veloce check` exits non-zero on a finding, so the audit gates deploys. Two
properties matter for that to be worth trusting. The set of checks is a
contract, so adding or dropping one is a deliberate act rather than a silent
change in what a clean run means. And a session backend written the documented
way - subclassing `SessionMiddlewareBase` - is audited like a built-in, because
a security check that passes in silence is worse than no check.
"""

from __future__ import annotations

from typing import Any

import pytest

from veloce import (
    SecurityHeadersMiddleware,
    ServerSessionMiddleware,
    SessionMiddleware,
    SessionMiddlewareBase,
    Veloce,
)
from veloce.middleware import Middleware

# The audit's covered set, keyed by the substring that identifies each finding.
# A new check adds a key here in the same change; a dropped one removes it.
# Whichever way it moves, the diff shows what a clean `veloce check` now means.
COVERED = {
    "debug": "DEBUG is enabled",
    "secret-key": "SECRET_KEY is not set",
    "session-cookie-secure": "SESSION_COOKIE_SECURE is off",
    "hardening-headers": "No SecurityHeadersMiddleware registered",
}


def _wide_open() -> Veloce:
    """An app that trips every check the audit makes."""
    app = Veloce(openapi_url=None, debug=True)
    app.config["SECRET_KEY"] = None
    app.add_middleware(SessionMiddleware(secret_key="k"))
    return app


def test_the_audit_covers_exactly_the_documented_set():
    warnings = _wide_open().security_audit()
    assert len(warnings) == len(COVERED), warnings
    for name, marker in COVERED.items():
        assert any(marker in w for w in warnings), f"{name} not reported: {warnings}"


def test_a_hardened_app_is_clean():
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(SessionMiddleware(secret_key="k"))
    app.use_secure_defaults()
    assert app.security_audit() == []


def test_use_secure_defaults_answers_every_finding_it_claims_to():
    """The remediation half must actually clear what the detection half flags."""
    app = _wide_open()
    app.config["SECRET_KEY"] = "k"
    app.debug = False
    app.use_secure_defaults()
    assert app.security_audit() == []


def test_use_secure_defaults_does_not_stack_a_second_headers_middleware():
    app = Veloce(openapi_url=None)
    app.use_secure_defaults()
    app.use_secure_defaults()
    installed = [m for m in app._middlewares if isinstance(m, SecurityHeadersMiddleware)]
    assert len(installed) == 1


# ── third-party backends ─────────────────────────────────────────────


class CustomSessionMiddleware(SessionMiddlewareBase):
    """A backend written the documented way - subclass, set the two lifetimes."""

    max_age = 3600
    permanent_lifetime = 2592000

    async def process_response(self, request: Any, response: Any) -> Any:
        return response


class DetachedSessionMiddleware(Middleware):
    """A backend that does not subclass the base. The audit cannot see it."""

    async def process_response(self, request: Any, response: Any) -> Any:
        return response


@pytest.mark.parametrize("backend", [SessionMiddleware(secret_key="k"), ServerSessionMiddleware()])
def test_both_built_in_backends_are_audited(backend):
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(backend)
    assert any("SESSION_COOKIE_SECURE is off" in w for w in app.security_audit())


def test_a_custom_backend_subclassing_the_base_is_audited():
    """The case that fails silently for anyone outside the family."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(CustomSessionMiddleware())
    assert any("SESSION_COOKIE_SECURE is off" in w for w in app.security_audit())


def test_the_base_carries_the_permanent_lifetime_rule_to_a_subclass():
    """Subclassing supplies the lifetime rule, not just the type."""
    mw = CustomSessionMiddleware()

    class _S:
        permanent = False

    session = _S()
    assert mw.cookie_lifetime(session) == 3600
    session.permanent = True
    assert mw.cookie_lifetime(session) == 2592000


def test_a_backend_outside_the_family_is_not_seen_and_that_is_documented():
    """Pins the stated boundary, so narrowing or widening it is deliberate."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(DetachedSessionMiddleware())
    assert not any("SESSION_COOKIE_SECURE" in w for w in app.security_audit())
    assert "cannot identify" in Veloce.security_audit.__doc__
