"""What `security_audit` covers, and what it admits it cannot see.

`veloce check` exits non-zero on a finding, so the audit gates deploys. Two
properties matter for that to be worth trusting. The set of checks is a
contract, so adding or dropping one is a deliberate act rather than a silent
change in what a clean run means. And a session backend written the documented
way - subclassing `SessionMiddlewareBase` - is audited like a built-in, because
a security check that passes in silence is worse than no check.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

import veloce.app.core
from veloce import (
    Finding,
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
    "session-cookie-secure": "The session cookie is not Secure",
    "hardening-headers": "No middleware sets hardening headers",
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
    """Hardening is now spelled where it takes effect - in each constructor."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(SessionMiddleware(secret_key="k", secure=True))
    app.add_middleware(
        SecurityHeadersMiddleware(
            hsts_max_age=31536000, content_security_policy="default-src 'self'"
        )
    )
    assert app.security_audit() == []


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
    assert any("The session cookie is not Secure" in w for w in app.security_audit())


def test_a_custom_backend_subclassing_the_base_is_audited():
    """The case that fails silently for anyone outside the family."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(CustomSessionMiddleware())
    assert any("The session cookie is not Secure" in w for w in app.security_audit())


def test_the_base_carries_the_permanent_lifetime_rule_to_a_subclass():
    """Subclassing supplies the lifetime rule, not just the type."""
    mw = CustomSessionMiddleware()

    class _S:
        permanent = False

    session = _S()
    assert mw.cookie_lifetime(session) == 3600
    session.permanent = True
    assert mw.cookie_lifetime(session) == 2592000


def test_a_middleware_declaring_nothing_is_not_audited():
    """A middleware that implements neither hook contributes nothing, by design."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(DetachedSessionMiddleware())
    assert not any("not Secure" in w for w in app.security_audit())


# ── the audit asks middleware, it does not name classes ──────────────


class ThirdPartyHeadersMiddleware(Middleware):
    """Hardening headers from outside this package - the marker is the claim."""

    sets_hardening_headers = True

    async def process_response(self, request: Any, response: Any) -> Any:
        return response


class ThirdPartyAuditedMiddleware(Middleware):
    """A middleware with a posture of its own, from outside this package."""

    async def process_response(self, request: Any, response: Any) -> Any:
        return response

    def audit(self, ctx: Any) -> Any:
        if not ctx.app.config.get("MY_TOKEN"):
            return (Finding("MY_TOKEN is not set.", "error", fix="set MY_TOKEN", id="my-token"),)
        return ()


def test_a_third_party_middleware_can_satisfy_the_hardening_check():
    """No isinstance against a built-in: the marker is what the audit asks."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(ThirdPartyHeadersMiddleware())
    assert not any("hardening headers" in w for w in app.security_audit())


def test_a_third_party_middleware_can_contribute_its_own_finding():
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(
        SecurityHeadersMiddleware(
            hsts_max_age=31536000, content_security_policy="default-src 'self'"
        )
    )
    app.add_middleware(ThirdPartyAuditedMiddleware())
    assert app.security_audit() == ["MY_TOKEN is not set. (set MY_TOKEN)"]
    app.config["MY_TOKEN"] = "t"
    assert app.security_audit() == []


def test_an_explicitly_secure_backend_is_not_warned_about():
    """The instance knows what the config key cannot: an explicit secure=True."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(SessionMiddleware(secret_key="k", secure=True))
    assert not any("not Secure" in w for w in app.security_audit())


def test_the_core_names_no_middleware_at_module_scope():
    """The inversion, asserted against the source rather than a load order.

    A module-level `from veloce.middleware...` in the app core means every
    app pays to import a middleware it may never register, and means the core
    knows a specific optional feature. Nothing in the core may import one.
    """
    core = pathlib.Path(veloce.app.core.__file__).read_text(encoding="utf-8")
    tree = ast.parse(core)
    offenders = [
        node.module
        for node in tree.body  # module scope only - nested imports are not walked
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("veloce.middleware")
        and node.module != "veloce.app.middleware"
    ]
    assert offenders == [], f"app/core.py imports middleware at module scope: {offenders}"


def test_the_audit_needs_no_middleware_class_to_run():
    """It reads the registered instances, so an empty stack is simply clean."""
    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    assert app.security_audit() == [
        "No middleware sets hardening headers - responses ship without nosniff, "
        "frame-deny or a referrer policy. "
        "(app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=31536000)))"
    ]
