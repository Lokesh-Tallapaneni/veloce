"""A route's response contract is reported by the one audit, with a severity.

`response_contract_audit()` was a second audit returning bare strings. Its own
docstring distinguished two severities in prose — a `response_model`
contradicting its return annotation was "a contradiction", an undocumented route
was "informational rather than a failure" — and then flattened both into one
untyped list.

Three things followed from that. The contradiction never reached `veloce check`'s
exit code, which is computed from the other audit alone; it could not be silenced,
because it had no id; and at startup it was gated on `debug` rather than on how
much it mattered. A route that declared one model and returned another exited 0.
"""

from __future__ import annotations

import sys

import pytest
from pydantic import BaseModel

from veloce import SecurityHeadersMiddleware, Veloce
from veloce.audit import run
from veloce.cli import main


class Alpha(BaseModel):
    a: int


class Beta(BaseModel):
    b: int


def _app(**kwargs) -> Veloce:
    app = Veloce(openapi_url=None, **kwargs)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(
        SecurityHeadersMiddleware(
            hsts_max_age=31536000, content_security_policy="default-src 'self'"
        )
    )
    return app


def _ids(app: Veloce) -> set[str]:
    return {f.id for f in run(app)}


# ── the contradiction ────────────────────────────────────────────────


def test_a_contradicting_contract_is_a_warning():
    """The defect: it was a bare string that no exit code read."""
    app = _app()

    @app.get("/x", response_model=Alpha)
    async def x() -> Beta:
        return Beta(b=1)

    findings = [f for f in run(app) if f.id == "response-model-contradiction"]
    assert [f.severity for f in findings] == ["warning"]


def test_the_contradiction_names_both_models():
    app = _app()

    @app.get("/x", response_model=Alpha)
    async def x() -> Beta:
        return Beta(b=1)

    finding = next(f for f in run(app) if f.id == "response-model-contradiction")
    assert "Alpha" in str(finding)
    assert "Beta" in str(finding)


def test_the_contradiction_fails_veloce_check(tmp_path, monkeypatch):
    """The property that was missing: it now reaches the exit code."""
    module = tmp_path / "contract_app.py"
    module.write_text(
        "from pydantic import BaseModel\n"
        "from veloce import SecurityHeadersMiddleware, Veloce\n"
        "class A(BaseModel):\n    a: int\n"
        "class B(BaseModel):\n    b: int\n"
        "app = Veloce(openapi_url=None)\n"
        'app.config["SECRET_KEY"] = "k"\n'
        "app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=1,"
        " content_security_policy=\"default-src 'self'\"))\n"
        '@app.get("/x", response_model=A)\n'
        "async def x() -> B:\n    return B(b=1)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("contract_app", None)
    assert main(["check", "contract_app:app"]) == 1


def test_a_matching_contract_reports_nothing():
    app = _app()

    @app.get("/x", response_model=Alpha)
    async def x() -> Alpha:
        return Alpha(a=1)

    assert "response-model-contradiction" not in _ids(app)


def test_a_response_model_with_no_annotation_is_not_a_contradiction():
    app = _app()

    @app.get("/x", response_model=Alpha)
    async def x():
        return {"a": 1}

    assert "response-model-contradiction" not in _ids(app)


def test_an_annotation_with_no_response_model_is_not_a_contradiction():
    app = _app()

    @app.get("/x")
    async def x() -> Alpha:
        return Alpha(a=1)

    assert "response-model-contradiction" not in _ids(app)


# ── the undocumented routes ──────────────────────────────────────────


def test_an_undocumented_route_is_informational():
    """Many are legitimate - HTML pages, redirects, streams."""
    app = _app()

    @app.get("/page")
    async def page():
        return "<html></html>"

    findings = [f for f in run(app) if f.id == "routes-undocumented"]
    assert [f.severity for f in findings] == ["info"]


def test_an_undocumented_route_does_not_fail_the_check():
    app = _app()

    @app.get("/page")
    async def page():
        return "<html></html>"

    assert [f for f in run(app) if f.at_least("warning")] == []


def test_the_undocumented_finding_counts_the_routes():
    app = _app()

    for n in range(3):

        @app.get(f"/p{n}", name=f"p{n}")
        async def page():
            return ""

    finding = next(f for f in run(app) if f.id == "routes-undocumented")
    assert "3 route(s)" in str(finding)


def test_a_long_list_of_undocumented_routes_is_truncated():
    app = _app()

    for n in range(15):

        @app.get(f"/p{n}", name=f"p{n}")
        async def page():
            return ""

    finding = next(f for f in run(app) if f.id == "routes-undocumented")
    assert "and 5 more" in str(finding)


def test_a_fully_documented_app_reports_neither():
    app = _app()

    @app.get("/x")
    async def x() -> Alpha:
        return Alpha(a=1)

    assert run(app) == []


# ── one audit, one vocabulary ────────────────────────────────────────


def test_both_findings_can_be_silenced():
    """The defect: they had no ids, so `SILENCED_AUDIT_IDS` could not reach them."""
    app = _app()
    app.config["SILENCED_AUDIT_IDS"] = ("response-model-contradiction", "routes-undocumented")

    @app.get("/x", response_model=Alpha)
    async def x() -> Beta:
        return Beta(b=1)

    @app.get("/page")
    async def page():
        return ""

    assert run(app) == []


def test_the_findings_appear_in_the_one_audit_listing():
    app = Veloce(openapi_url=None)

    @app.get("/x", response_model=Alpha)
    async def x() -> Beta:
        return Beta(b=1)

    ids = _ids(app)
    assert "response-model-contradiction" in ids
    assert "hardening-headers-missing" in ids


# ── the public method still works ────────────────────────────────────


def test_the_public_method_renders_only_contract_findings():
    app = Veloce(openapi_url=None)

    @app.get("/x", response_model=Alpha)
    async def x() -> Beta:
        return Beta(b=1)

    rendered = app.response_contract_audit()
    assert len(rendered) == 1
    assert "Alpha" in rendered[0]
    # The security posture is not mixed in.
    assert not any("hardening headers" in line for line in rendered)


def test_the_public_method_returns_strings():
    app = _app()

    @app.get("/page")
    async def page():
        return ""

    rendered = app.response_contract_audit()
    assert rendered and all(isinstance(line, str) for line in rendered)


def test_the_public_method_is_empty_for_a_documented_app():
    app = _app()

    @app.get("/x")
    async def x() -> Alpha:
        return Alpha(a=1)

    assert app.response_contract_audit() == []


@pytest.mark.parametrize("silenced", [True, False])
def test_the_public_method_follows_silencing(silenced):
    """It renders the audit, so it inherits the audit's behaviour."""
    app = _app()
    if silenced:
        app.config["SILENCED_AUDIT_IDS"] = ("routes-undocumented",)

    @app.get("/page")
    async def page():
        return ""

    assert bool(app.response_contract_audit()) is not silenced
