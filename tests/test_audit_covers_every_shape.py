"""Everything registered on the app can report to `veloce check`.

Veloce accepts middleware in three shapes — `Middleware` instances,
`BaseHTTPMiddleware` dispatch objects, and ASGI middleware classes — and static
handlers besides. The audit walked one of them.

So a dispatch-shape middleware that hardened every response was reported as
absent, which is a false failure on a correctly configured app; and a
`StaticFiles` pointed at a directory that does not exist warned once at
construction, where nothing running `veloce check` would ever see it.
"""

from __future__ import annotations

import warnings

import pytest

from veloce import BaseHTTPMiddleware, Middleware, SecurityHeadersMiddleware, Veloce
from veloce.audit import Finding, run
from veloce.contrib.staticfiles import StaticFiles
from veloce.middleware.base import Auditable
from veloce.testclient import TestClient


class HardeningDispatch(BaseHTTPMiddleware):
    """A dispatch-shape middleware that really does set the headers."""

    sets_hardening_headers = True

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


class ReportingDispatch(BaseHTTPMiddleware):
    """A dispatch-shape middleware with a finding of its own."""

    async def dispatch(self, request, call_next):
        return await call_next(request)

    def audit(self, ctx):
        if not ctx.app.config.get("MY_TOKEN"):
            yield Finding("MY_TOKEN is not set.", "warning", fix="set MY_TOKEN", id="my-token")


class HardeningASGI:
    """An ASGI middleware class - never instantiated until the stack is built."""

    sets_hardening_headers = True

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


# ── the contract is declared once, for every shape ───────────────────


@pytest.mark.parametrize("cls", [Middleware, BaseHTTPMiddleware])
def test_every_middleware_shape_carries_the_audit_contract(cls):
    assert issubclass(cls, Auditable)
    assert cls.sets_hardening_headers is False
    assert cls.audit_needs_routes is False


def test_the_contract_is_defined_once():
    """Two copies of a declaration drift; the mixin owns it."""
    assert "audit" in Auditable.__dict__
    assert "audit" not in Middleware.__dict__
    assert "audit" not in BaseHTTPMiddleware.__dict__


# ── the false failure ────────────────────────────────────────────────


def test_a_dispatch_middleware_can_satisfy_the_hardening_check():
    """The defect: this app really is hardened and was reported as not."""
    app = Veloce(openapi_url=None)
    app.add_http_middleware(HardeningDispatch())

    @app.get("/x")
    async def x():
        return {}

    assert TestClient(app).get("/x").headers["X-Content-Type-Options"] == "nosniff"
    assert [f.id for f in run(app)] == []


def test_an_asgi_middleware_class_can_satisfy_the_hardening_check():
    """Held as a class, so only its class-level marker can be read."""
    app = Veloce(openapi_url=None)
    app.add_middleware(HardeningASGI)
    assert "hardening-headers-missing" not in {f.id for f in run(app)}


def test_a_dispatch_middleware_contributes_its_own_finding():
    app = Veloce(openapi_url=None)
    app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=31536000))
    app.add_http_middleware(ReportingDispatch())
    assert "my-token" in {f.id for f in run(app)}
    app.config["MY_TOKEN"] = "t"
    assert "my-token" not in {f.id for f in run(app)}


def test_a_plain_function_middleware_is_skipped_not_crashed():
    """`@app.middleware("http")` registers a bare function with no hooks."""
    app = Veloce(openapi_url=None)

    @app.middleware("http")
    async def passthrough(request, call_next):
        return await call_next(request)

    assert [f.id for f in run(app)] == ["hardening-headers-missing"]


# ── static handlers ──────────────────────────────────────────────────


def test_a_missing_static_directory_reaches_the_audit(tmp_path):
    """A `warnings.warn` at construction never reaches `veloce check`."""
    app = Veloce(openapi_url=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        app.mount("/assets", StaticFiles(directory=str(tmp_path / "nope"), must_exist=False))
    findings = {f.id: f for f in run(app)}
    assert "static-directory-missing" in findings
    assert findings["static-directory-missing"].severity == "info"
    assert "/assets" in str(findings["static-directory-missing"])


def test_a_present_static_directory_reports_nothing(tmp_path):
    (tmp_path / "assets").mkdir()
    app = Veloce(openapi_url=None)
    app.mount("/assets", StaticFiles(directory=str(tmp_path / "assets")))
    assert "static-directory-missing" not in {f.id for f in run(app)}


def test_a_missing_static_directory_is_informational_not_fatal(tmp_path):
    """`must_exist=False` exists for the dev flow that creates it later."""
    app = Veloce(openapi_url=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        app.mount("/assets", StaticFiles(directory=str(tmp_path / "nope"), must_exist=False))

    @app.get("/x")
    async def x():
        return {}

    assert TestClient(app).get("/x").status_code == 200


def test_must_exist_still_raises_at_construction(tmp_path):
    """The loud path is unchanged; the audit covers only the downgraded one."""
    with pytest.raises(ValueError, match="does not exist"):
        StaticFiles(directory=str(tmp_path / "nope"))


# ── the shapes compose ───────────────────────────────────────────────


def test_findings_from_several_shapes_are_all_collected(tmp_path):
    app = Veloce(openapi_url=None)
    app.add_http_middleware(ReportingDispatch())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        app.mount("/assets", StaticFiles(directory=str(tmp_path / "nope"), must_exist=False))
    ids = {f.id for f in run(app)}
    assert {"my-token", "static-directory-missing", "hardening-headers-missing"} <= ids


def test_an_empty_app_still_reports_only_the_absence_check():
    assert [f.id for f in run(Veloce(openapi_url=None))] == ["hardening-headers-missing"]


def test_a_finding_from_any_shape_can_be_silenced():
    app = Veloce(openapi_url=None)
    app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=31536000))
    app.add_http_middleware(ReportingDispatch())
    app.config["SILENCED_AUDIT_IDS"] = ("my-token",)
    assert "my-token" not in {f.id for f in run(app)}
