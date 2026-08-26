"""Each app mixin's `TYPE_CHECKING` host manifest is checked, not just written.

`app/` is a package of mixins whose state all lives on the host class in
`core.py`. mypy checking a mixin in isolation cannot see that state, so each
mixin re-declares the host attributes it borrows in a class-level
`if TYPE_CHECKING:` block. Those blocks are hand-maintained and, being invisible
at runtime, nothing ever contradicted them: they had accumulated ten
declarations for attributes the module no longer touched, one of them
(`_mcp_context`) for something that is not an app attribute at all - it is a
slot on `DependencyResolver`.

A stale manifest is not inert. It is what a reader consults to learn what a
mixin depends on, and it silences mypy: declaring `foo: Any` makes `self.foo`
type-check in that module whether or not the host still has a `foo`, so a
renamed or deleted host attribute produces no error at the borrowing site.

These tests make the manifest an assertion. Every declared name must be used by
its own module and must exist on a real application, so the block can only
describe what is true.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from veloce import Veloce

_APP_DIR = Path(__file__).resolve().parents[1] / "src" / "veloce" / "app"
_DECL = re.compile(r"^        ([A-Za-z_][A-Za-z0-9_]*)\s*:")


def _manifest(path: Path) -> list[str]:
    """The names declared in the module's class-level `TYPE_CHECKING` block."""
    names: list[str] = []
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^    if TYPE_CHECKING:", line):
            inside = True
            continue
        if not inside:
            continue
        match = _DECL.match(line)
        if match:
            names.append(match.group(1))
        elif line.strip() and not line.lstrip().startswith("#"):
            inside = False
    return names


_MODULES = sorted(p for p in _APP_DIR.glob("*.py") if _manifest(p))
_DECLARATIONS = sorted((p, name) for p in _MODULES for name in _manifest(p))
_IDS = [f"{p.name}:{name}" for p, name in _DECLARATIONS]


def _app() -> Veloce:
    return Veloce(openapi_url=None)


def _defined_on_app(name: str, app: Veloce) -> bool:
    """Presence without triggering properties - `jinja_env` raises when unbound."""
    return name in vars(app) or any(name in vars(base) for base in type(app).__mro__)


# ── the manifest is not vacuous ──────────────────────────────────────


def test_the_mixins_carry_manifests():
    """If the blocks were renamed away this module must fail, not pass empty."""
    assert len(_MODULES) >= 10
    assert len(_DECLARATIONS) >= 100


def test_the_parser_finds_a_known_declaration():
    """A named landmark, so a broken parser cannot make every test below pass."""
    assert "_middlewares" in _manifest(_APP_DIR / "dispatch.py")


# ── every declared name is one the module actually borrows ───────────


@pytest.mark.parametrize(("path", "name"), _DECLARATIONS, ids=_IDS)
def test_a_declared_attribute_is_used_by_its_own_module(path, name):
    """The fossil check: ten declarations had no `self.<name>` left in the file.

    A manifest entry exists to type an access. With no access it documents a
    dependency the mixin does not have, and keeps a name alive that the host is
    free to have dropped.
    """
    text = path.read_text(encoding="utf-8")
    uses = re.findall(rf"self\.{re.escape(name)}\b", text)
    assert uses, f"{path.name} declares `{name}` but never reads it through `self`"


# ── and every declared name really is on the host ────────────────────


@pytest.mark.parametrize(("path", "name"), _DECLARATIONS, ids=_IDS)
def test_a_declared_attribute_exists_on_a_real_application(path, name):
    """The drift check: `_mcp_context` was declared on the app and lives on the
    dependency resolver, so the declaration typed an access that would have
    raised."""
    assert _defined_on_app(name, _app()), (
        f"{path.name} declares host attribute `{name}`, which no Veloce defines"
    )


# ── the negative: the checks above can fail ──────────────────────────


def test_an_unused_declaration_would_be_caught(tmp_path):
    module = tmp_path / "probe.py"
    module.write_text(
        "class M:\n"
        "    if TYPE_CHECKING:\n"
        "        used: Any\n"
        "        unused: Any\n"
        "\n"
        "    def m(self):\n"
        "        return self.used\n",
        encoding="utf-8",
    )
    declared = _manifest(module)
    assert declared == ["used", "unused"]
    text = module.read_text(encoding="utf-8")
    assert re.findall(r"self\.used\b", text)
    assert not re.findall(r"self\.unused\b", text)


def test_a_nonexistent_host_attribute_would_be_caught():
    assert not _defined_on_app("_no_such_attribute_anywhere", _app())


def test_a_real_host_attribute_is_recognised():
    """The other direction, so the presence check is not always-false."""
    assert _defined_on_app("config", _app())
    assert _defined_on_app("_middlewares", _app())
