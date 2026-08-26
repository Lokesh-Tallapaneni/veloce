"""The policy page's list of gateways matches the gateways that exist.

`docs/policies.md` defines the public API as the top-level `__all__` plus "the
same names re-exported from each subpackage gateway", and then lists the
gateways. The list omitted two that the code and the documentation both treat as
public:

* `veloce.app`, which exports **`Veloce` itself**;
* `veloce.contrib.mcp`, which exports 63 documented names.

Everything not on that list is declared private and "may change or be removed at
any time without notice" - so by the page's own wording, `Veloce` was private.

This test keeps the prose and the code in step: a package that declares `__all__`
and is reachable as a gateway must be named in the policy, and the policy must
not name a gateway that does not exist.
"""

from __future__ import annotations

import importlib
import pathlib
import re

import pytest

POLICY = pathlib.Path(__file__).resolve().parent.parent / "docs" / "policies.md"

# Gateways the policy is expected to enumerate. `veloce.sessions` and the
# private `_*` modules are not gateways: they declare no `__all__`.
GATEWAYS = [
    "veloce.app",
    "veloce.http",
    "veloce.routing",
    "veloce.middleware",
    "veloce.security",
    "veloce.contrib",
    "veloce.contrib.mcp",
    "veloce.serving",
]


def _listed_gateways() -> set[str]:
    text = POLICY.read_text(encoding="utf-8")
    section = text[text.index("## What is the public API") :]
    section = section[: section.index("\n## ")]
    found = set(re.findall(r"`(veloce(?:\.[a-z_]+)+)`", section))
    # `veloce.__all__` is named in the prose as the top-level export list, not
    # as a gateway module.
    return {name for name in found if not name.endswith("__all__")}


def test_the_policy_page_names_every_gateway():
    """The defect: `veloce.app` and `veloce.contrib.mcp` were missing."""
    missing = sorted(set(GATEWAYS) - _listed_gateways())
    assert not missing, f"gateways the policy does not name as public: {missing}"


def test_the_policy_page_names_no_gateway_that_is_not_one():
    listed = _listed_gateways()
    for name in sorted(listed):
        module = importlib.import_module(name)
        assert getattr(module, "__all__", None), f"{name} is named but declares no __all__"


@pytest.mark.parametrize("name", GATEWAYS)
def test_each_gateway_declares_an_all(name):
    """The premise: these are gateways because they curate `__all__`."""
    module = importlib.import_module(name)
    assert getattr(module, "__all__", None), name


def test_veloce_itself_is_covered_by_the_policy():
    """The sharp end: `Veloce` is exported from `veloce.app`, so a policy that
    omits that gateway declares the framework's own entry point private."""
    import veloce.app

    assert "Veloce" in veloce.app.__all__
    assert "veloce.app" in _listed_gateways()


def test_the_section_was_actually_found():
    """A parse that matched nothing would make every assertion above vacuous."""
    assert len(_listed_gateways()) >= len(GATEWAYS)
