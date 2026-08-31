"""A test module is named for a subject, and nothing else.

The suite is one flat directory of ~684 modules. The rubric calls that a finding
on its own, and browsing it is genuinely awkward - but sub-packaging is not the
fix here, for a reason the numbers make plain: of the prefixes in use, **140 are
singletons**. Grouping by prefix puts 544 modules in a real home and leaves 140
in a junk drawer, which is precisely the `test_polish_small_e2e.py` problem
(findings 41, 46, 66, 78, 82) rebuilt as directories. A file lives in one
directory; its prefix travels with it into every test id, traceback and
`-k` expression.

What the flat layout was actually missing is that nothing **enforced** the
convention. Every batch-named module this review found - named for an audit
round, a fix batch, an issue number - was possible because a new file could be
called anything. That is checkable, and checking it is what these tests do.

They are deliberately narrow. A name is rejected only for saying something other
than a subject: a batch, a date, an issue number, or a catch-all word. Everything
else passes, because inventing a taxonomy and policing membership is the
directory problem again.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

import veloce

TESTS = pathlib.Path(__file__).resolve().parent

# Words that name a work item rather than a subject. Each of these was the name
# of a real module in this repository before this review.
BATCH_WORDS = {
    "batch",
    "findings",
    "fixes",
    "gaps",
    "issues",
    "iteration",
    "misc",
    "polish",
    "round",
    "scratch",
    "sweep",
    "unswept",
    "wave",
}

# Catch-alls: a module named this can absorb anything, which is how they grow.
CATCH_ALL = {"bugs", "coverage", "extra", "final", "more", "new", "other", "reliability"}

# Not a word boundary before the year: the character before it is usually `_`,
# which is a word character, so the boundary never matches in
# `test_audit_2026_08`.
_DATE = re.compile(r"(?<![0-9])(19|20)\d{2}[-_]?\d{2}")
_ISSUE = re.compile(r"(?:^|_)(?:issue|pr|ticket)[-_]?\d+")


def _modules() -> list[pathlib.Path]:
    return sorted(TESTS.rglob("test_*.py"))


def _words(path: pathlib.Path) -> list[str]:
    return path.stem[len("test_") :].split("_")


def _public_names() -> set[str]:
    """Lowercased names of everything the package exports, plus its own words.

    A word that is a *feature* is not a batch word, whatever it looks like.
    `Finding` is the class `app.security_audit()` yields, so
    `test_audit_findings.py` is named for its subject; `extra` is the
    `Veloce(**extra)` passthrough and the route-level `openapi_extra` option.
    Checking against the real public surface is what tells those apart from
    `test_unswept_scope_findings.py`, and it cannot go stale the way a
    hand-written exemption list would.
    """
    names = {name.lower() for name in veloce.__all__}
    # A plural of a public name is the same name. `Finding` is the class; a
    # module covering several of them is `..._findings`.
    names.update(name + "s" for name in list(names))
    names.update(inspect.signature(veloce.Veloce.__init__).parameters)
    names.update(inspect.signature(veloce.Router.add_route).parameters)
    return names


EXEMPT = _public_names()


def _complaint(path: pathlib.Path) -> str | None:
    """Why `path`'s name is not a subject, or `None` if it is.

    One function rather than five parameterised checks: five would be 3,400 test
    cases over a 686-module suite to answer one question per file.
    """
    words = _words(path)
    batch = sorted(BATCH_WORDS.intersection(words) - EXEMPT)
    if batch:
        return (
            f"named for the work that produced it, not for what it covers: "
            f"{batch}. Name it for the subject."
        )
    last = words[-1]
    if last in CATCH_ALL and last not in EXEMPT:
        return f"ends in the catch-all word {last!r} - it can absorb anything, and will"
    if _DATE.search(path.stem):
        return "named for when it was written"
    if _ISSUE.search(path.stem):
        return "named for a tracker item"
    if len(path.stem) <= len("test_") + 2:
        return "says nothing"
    return None


def test_a_module_is_named_for_its_subject():
    """One scan of the corpus; the message names every offender."""
    offenders = [f"{path.name} is {found}" for path in _modules() if (found := _complaint(path))]
    assert offenders == [], offenders


# ── the checks are not vacuous ───────────────────────────────────────


def test_the_naming_scan_covers_the_suite():
    assert len(_modules()) > 400


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("test_unswept_scope_findings.py", "work item"),
        ("test_polish_small_e2e.py", "work item"),
        ("test_batch_4_fixes.py", "work item"),
        ("test_final_coverage.py", "catch-all"),
        ("test_reported_bugs.py", "catch-all"),
        ("test_audit_2026_08.py", "date"),
        ("test_issue_412.py", "tracker item"),
        ("test_ab.py", "says nothing"),
    ],
)
def test_a_bad_name_is_reported(name, why):
    """Every rule is exercised, so none can quietly stop matching."""
    complaint = _complaint(pathlib.Path(name))
    assert complaint is not None, f"{name} should have been rejected ({why})"


@pytest.mark.parametrize(
    "name",
    [
        "test_websocket_framing.py",
        "test_session_middleware.py",
        "test_mcp_hardening.py",
        "test_request_mimetype.py",
        "test_server_tcp_keepalive.py",
        "test_audit_findings.py",
        "test_openapi_extra.py",
        "test_oauth2_extra_schemes.py",
        "test_native_refusal_response_phase.py",
    ],
)
def test_a_real_name_is_accepted(name):
    """The other direction, including the four the first draft of these rules
    rejected wrongly: `findings` is the class `security_audit()` yields, `extra`
    is the `Veloce(**extra)` passthrough and the `openapi_extra` route option,
    and a response *phase* is a real concept."""
    assert _complaint(pathlib.Path(name)) is None
