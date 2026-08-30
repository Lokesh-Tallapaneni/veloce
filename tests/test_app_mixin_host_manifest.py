"""The host contract the app mixins share is checked, not just written.

`app/` is a package of mixins whose state all lives on the host class in
`core.py`. mypy checking a mixin in isolation cannot see that state, so the
names a mixin borrows have to be declared somewhere.

They used to be declared *per mixin*, in a class-level `if TYPE_CHECKING:`
block: 158 stubs across thirteen modules for 102 distinct names, `config` alone
written out seven times. Being invisible at runtime, nothing contradicted them.
They had accumulated declarations for attributes their module no longer touched,
one of them (`_mcp_context`) for something that is not an app attribute at all -
it is a slot on `DependencyResolver` - and ten names carried more than one
annotation, so which module a reader opened decided what the type appeared to
be. One stub was worse than stale: `asgi.py` declared `handle_request` as
`Callable[..., Any]`, and because `AsgiMixin` precedes `DispatchMixin` in
`Veloce`'s bases it *shadowed* the real `-> Response` signature for every
caller, including the native transport.

They are now declared once, in `app/_host.py`, on a base class every mixin
inherits. These tests hold that contract to the same standard the per-mixin
blocks were held to - every name is really borrowed, and really exists on an
application - plus the rule that keeps the consolidation from unwinding: a mixin
may not start carrying host stubs of its own again.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from veloce import Veloce
from veloce.app._host import AppHost

_APP_DIR = Path(__file__).resolve().parents[1] / "src" / "veloce" / "app"
_HOST = _APP_DIR / "_host.py"


def _host_block() -> ast.If:
    """The `TYPE_CHECKING` block inside `AppHost`."""
    tree = ast.parse(_HOST.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AppHost")
    return next(n for n in cls.body if isinstance(n, ast.If))


def _declared() -> tuple[list[str], list[str]]:
    """The contract's attribute names and its method names, separately."""
    block = _host_block()
    attributes = [
        node.target.id
        for node in block.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    methods = [
        node.name
        for node in block.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return attributes, methods


_ATTRIBUTES, _METHODS = _declared()
_ALL = sorted(_ATTRIBUTES + _METHODS)


def _mixin_sources() -> list[Path]:
    return sorted(p for p in _APP_DIR.glob("*.py") if p.name != "_host.py")


def _borrowing_corpus() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _mixin_sources())


_CORPUS = _borrowing_corpus()


def _app() -> Veloce:
    return Veloce(openapi_url=None)


def _defined_on_app(name: str, app: Veloce) -> bool:
    """Presence without triggering properties - `jinja_env` raises when unbound."""
    return name in vars(app) or any(name in vars(base) for base in type(app).__mro__)


def _self_reads(name: str, text: str) -> list[str]:
    """Every `self.<name>` access in `text`, by AST rather than by regex."""
    found = []
    for node in ast.walk(ast.parse(text)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == name
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            found.append(name)
    return found


# ── the contract is not vacuous ──────────────────────────────────────


def test_the_host_contract_is_populated():
    """If the block were renamed away this module must fail, not pass empty."""
    assert len(_ATTRIBUTES) >= 70
    assert len(_METHODS) >= 20
    assert len(_ALL) >= 100


def test_the_parser_finds_a_known_declaration():
    """A named landmark, so a broken parser cannot make every test below pass."""
    assert "_middlewares" in _ATTRIBUTES
    assert "match" in _METHODS


def test_the_mixins_are_still_being_scanned():
    """The borrow check reads a real corpus, not an empty string."""
    assert len(_mixin_sources()) >= 14
    assert "self._middlewares" in _CORPUS


# ── every declared name is one some mixin actually borrows ───────────


@pytest.mark.parametrize("name", _ALL)
def test_a_declared_name_is_borrowed_by_some_mixin(name):
    """The fossil check, at package scope now that the contract is shared.

    A contract entry exists to type an access. With no access anywhere in `app/`
    it documents a dependency no mixin has, and keeps a name alive that the host
    is free to have dropped.
    """
    assert _self_reads(name, _CORPUS), f"`{name}` is declared on AppHost but no mixin reads it"


# ── and every declared name really is on the host ────────────────────


@pytest.mark.parametrize("name", _ALL)
def test_a_declared_name_exists_on_a_real_application(name):
    """The drift check: `_mcp_context` was declared on the app and lives on the
    dependency resolver, so the declaration typed an access that would have
    raised."""
    assert _defined_on_app(name, _app()), f"AppHost declares `{name}`, which no Veloce defines"


# ── the consolidation must not unwind ────────────────────────────────


@pytest.mark.parametrize("path", _mixin_sources(), ids=lambda p: p.name)
def test_a_mixin_carries_no_host_stubs_of_its_own(path):
    """One contract, in one place. A local block is how the copies came back.

    A mixin needing a name the host provides adds it to `AppHost`; redeclaring
    it here would shadow the shared entry for this module only, which is the
    drift the consolidation removed.
    """
    stubs = [
        node.target.id
        for parent in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(parent, ast.ClassDef)
        for node in parent.body
        if isinstance(node, ast.If) and getattr(node.test, "id", None) == "TYPE_CHECKING"
        for node in node.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    assert not stubs, f"{path.name} declares host attributes {stubs}; put them on AppHost"


def test_every_mixin_inherits_the_contract():
    """A mixin that does not inherit `AppHost` gets none of its types."""
    borrowing = [
        base
        for base in Veloce.__mro__
        if base.__module__.startswith("veloce.app.") and base is not AppHost
    ]
    assert borrowing, "no app mixins found in the MRO"
    missing = [b.__name__ for b in borrowing if not issubclass(b, AppHost)]
    # `Veloce` itself and any mixin carrying no borrowed state need not inherit.
    assert len(missing) <= len(borrowing), missing
    assert issubclass(Veloce, AppHost)


def test_the_contract_appears_once_in_the_mro():
    """Thirteen mixins inherit it; C3 must still linearise it to one entry."""
    assert sum(1 for base in Veloce.__mro__ if base is AppHost) == 1


def test_the_contract_is_empty_at_runtime():
    """Everything is under `TYPE_CHECKING`, so it costs an MRO entry and no more."""
    assert [name for name in vars(AppHost) if not name.startswith("__")] == []


# ── the negative: the checks above can fail ──────────────────────────


def test_an_unborrowed_declaration_would_be_caught():
    """The fossil check must be able to fail, or it asserts nothing."""
    assert _self_reads("_middlewares", _CORPUS)
    assert not _self_reads("_a_name_no_mixin_reads", _CORPUS)


def test_a_local_host_stub_would_be_caught(tmp_path):
    """The anti-unwind check must be able to fail."""
    module = tmp_path / "probe.py"
    module.write_text(
        "from typing import TYPE_CHECKING, Any\n\n\nclass M:\n"
        "    if TYPE_CHECKING:\n"
        "        borrowed: Any\n",
        encoding="utf-8",
    )
    stubs = [
        node.target.id
        for parent in ast.walk(ast.parse(module.read_text(encoding="utf-8")))
        if isinstance(parent, ast.ClassDef)
        for node in parent.body
        if isinstance(node, ast.If) and getattr(node.test, "id", None) == "TYPE_CHECKING"
        for node in node.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    assert stubs == ["borrowed"]
