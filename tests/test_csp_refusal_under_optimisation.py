"""`CSPMiddleware` refuses an empty configuration even with asserts stripped.

Named `test_csp_requires_a_policy_under_o.py` until now - a sentence truncated
mid-word, which said neither what it covered nor what "under o" meant.

The constructor validated its arguments with `assert`:

    assert policy is not None or report_only_policy is not None, (
        "CSPMiddleware requires at least one of policy or report_only_policy"
    )

`python -O` removes assert statements. Under it, `CSPMiddleware()` constructed
happily and then emitted **no Content-Security-Policy header at all** - an
operator who put the middleware in their stack had no CSP, no error and no
warning. A security control that fails open when the interpreter is optimised is
worse than one that is absent, because the stack says it is there.

It raises `ValueError` now, which survives `-O`.

This is a narrow exception to the project's convention of `AssertionError` for
API misuse: that convention is fine where a stripped check costs a worse error
message later, and wrong where it costs a silently disabled security header. The
other asserts in this file and in `compression.py` are type-narrowing on values
already established non-`None`, so stripping them changes nothing.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from veloce import CSPMiddleware, Veloce
from veloce.testclient import TestClient


def test_no_policy_is_refused():
    """The defect: this was an assert, and `-O` removed it."""
    with pytest.raises(ValueError, match="policy"):
        CSPMiddleware()


def test_the_error_names_both_arguments():
    with pytest.raises(ValueError) as excinfo:
        CSPMiddleware()
    message = str(excinfo.value)
    assert "policy" in message and "report_only_policy" in message


def test_an_enforced_policy_is_accepted():
    assert CSPMiddleware("default-src 'self'") is not None


def test_a_report_only_policy_alone_is_accepted():
    assert CSPMiddleware(report_only_policy="default-src 'self'") is not None


def test_both_together_are_accepted():
    assert CSPMiddleware("default-src 'self'", report_only_policy="script-src 'none'") is not None


def test_an_enforced_policy_still_emits_the_header():
    """The negative: the guard must not have broken the working case."""
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware("default-src 'self'"))

    @app.get("/")
    async def index() -> dict:
        return {}

    headers = TestClient(app).get("/").headers
    assert any(k.lower() == "content-security-policy" for k in headers)


def test_a_report_only_policy_emits_the_report_header():
    app = Veloce(openapi_url=None)
    app.add_middleware(CSPMiddleware(report_only_policy="default-src 'self'"))

    @app.get("/")
    async def index() -> dict:
        return {}

    headers = TestClient(app).get("/").headers
    assert any(k.lower() == "content-security-policy-report-only" for k in headers)


# ── the property that matters: it survives -O ────────────────────────


def _run_optimised(body: str) -> subprocess.CompletedProcess:
    """Run `body` in a subprocess with `-O`, where asserts are stripped."""
    return subprocess.run(
        [sys.executable, "-O", "-c", textwrap.dedent(body)],
        capture_output=True,
        text=True,
    )


def test_asserts_really_are_stripped_under_o():
    """Pins the premise - if this stopped holding, the test below proves nothing."""
    result = _run_optimised(
        """
        try:
            assert False, "boom"
            print("STRIPPED")
        except AssertionError:
            print("KEPT")
        """
    )
    assert result.stdout.strip() == "STRIPPED", result.stderr


def test_an_empty_configuration_is_refused_under_o():
    """The defect, at the interpreter setting that exposed it."""
    result = _run_optimised(
        """
        from veloce import CSPMiddleware
        try:
            CSPMiddleware()
            print("CONSTRUCTED")
        except ValueError:
            print("REFUSED")
        """
    )
    assert result.stdout.strip() == "REFUSED", result.stderr or result.stdout


def test_a_valid_policy_still_emits_its_header_under_o():
    """The other direction: the guard must not refuse a good configuration."""
    result = _run_optimised(
        """
        from veloce import CSPMiddleware, Veloce
        from veloce.testclient import TestClient

        app = Veloce(openapi_url=None)
        app.add_middleware(CSPMiddleware("default-src 'self'"))

        @app.get("/")
        async def index() -> dict:
            return {}

        headers = TestClient(app).get("/").headers
        print("CSP" if any(k.lower() == "content-security-policy" for k in headers) else "NONE")
        """
    )
    assert result.stdout.strip().endswith("CSP"), result.stderr or result.stdout
