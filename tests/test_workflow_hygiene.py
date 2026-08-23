"""CI workflows pin what they run and ask for no more than they need.

A workflow that references a third-party action by tag runs whatever that tag
points at today, so a compromised or retagged action executes with the
workflow's token. Pinning to a commit makes the reference immutable, and
Dependabot is already configured for `github-actions`, so the pins stay current
without hand-maintenance.

The rest is blast radius: a default-scoped token, a token persisted into
`.git/config` where later steps can read it, and a job with no wall-clock
ceiling all widen what a single bad step can do.
"""

from __future__ import annotations

import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

_WORKFLOW_DIR = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"

# `.github/` is repository metadata, not part of the distributed package, so the
# suite can legitimately run somewhere it does not exist (an sdist, an exported
# tree). Skip there rather than fail; where the directory is present, the
# assertions below are exact.
pytestmark = pytest.mark.skipif(
    not _WORKFLOW_DIR.is_dir(), reason="no .github/workflows in this tree"
)

_WORKFLOWS = sorted(_WORKFLOW_DIR.glob("*.yml"))

#: A 40-character hex commit, optionally followed by a `# vN` readability tag.
_PINNED = re.compile(r"^[^@]+@[0-9a-f]{40}(\s+#.*)?$")


def _documents():
    return [(p.name, yaml.safe_load(p.read_text(encoding="utf-8"))) for p in _WORKFLOWS]


def test_there_are_workflows_to_check():
    """A glob that matched nothing would make every test below vacuous."""
    assert _WORKFLOWS, "no workflow files found"


@pytest.mark.parametrize("path", _WORKFLOWS, ids=lambda p: p.name)
def test_every_external_action_is_pinned_to_a_commit(path: pathlib.Path):
    unpinned = [
        ref
        for ref in re.findall(r"uses:\s*(\S.*)$", path.read_text(encoding="utf-8"), re.M)
        # A local reusable workflow is versioned by this repository already.
        if not ref.startswith("./") and not _PINNED.match(ref.strip())
    ]
    assert not unpinned, f"{path.name}: pin to a commit SHA: {unpinned}"


@pytest.mark.parametrize(
    ("name", "doc"), _documents(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_workflow_declares_its_permissions(name: str, doc: dict):
    """Without one, the token carries the repository default."""
    jobs = doc.get("jobs", {})
    if "permissions" in doc:
        return
    missing = [job for job, spec in jobs.items() if "permissions" not in spec]
    assert not missing, f"{name}: no top-level permissions, and jobs missing one: {missing}"


@pytest.mark.parametrize(
    ("name", "doc"), _documents(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_job_has_a_wall_clock_ceiling(name: str, doc: dict):
    missing = [
        job
        for job, spec in doc.get("jobs", {}).items()
        # A job that only calls a reusable workflow inherits that workflow's.
        if "timeout-minutes" not in spec and "uses" not in spec
    ]
    assert not missing, f"{name}: jobs without timeout-minutes: {missing}"


@pytest.mark.parametrize("path", _WORKFLOWS, ids=lambda p: p.name)
def test_no_checkout_persists_its_credentials(path: pathlib.Path):
    """Left on, the token lands in `.git/config` for every later step to read."""
    text = path.read_text(encoding="utf-8")
    checkouts = text.count("uses: actions/checkout@")
    if not checkouts:
        return
    assert text.count("persist-credentials: false") >= checkouts, (
        f"{path.name}: {checkouts} checkout(s), "
        f"{text.count('persist-credentials: false')} with persist-credentials: false"
    )
