"""No test reaches `request._state` when `request.state` is the same object.

`Request.state` is a public property whose whole body is `return self._state`,
so the private name was never buying anything - and the two spellings had
drifted apart in neighbouring modules, with the ProxyFix client hop written one
way in one and the other way in the next. Fifty-one sites across fifteen
modules now use the property.

This is deliberately narrow. Tests do legitimately reach private state on
framework objects - `ws._accepted`, `ws._closed`, `response._encoded` - because
no public equivalent exists and inventing one would put "pretend you
handshook" in the supported surface. `_state` is the case where the public
equivalent already exists and returns the identical object, which is the only
case this guard covers.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests.conftest import make_request

TESTS = pathlib.Path(__file__).resolve().parent


def _modules() -> list[pathlib.Path]:
    return sorted(TESTS.rglob("test_*.py"))


def _private_state_accesses(path: pathlib.Path) -> list[int]:
    """Line numbers of `<anything>._state` in code (not in prose)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "_state"
    ]


def test_the_request_state_scan_reads_a_real_corpus():
    """A scan of nothing passes every check below it.

    The glob is non-recursive and hard-codes the flat layout, so moving the
    test tree into subdirectories would leave this module green while reading
    no files at all.
    """
    assert len(_modules()) > 100, (
        "the module glob matched almost nothing, so the request-state scan reads nothing"
    )


def test_the_property_returns_the_private_attribute_unchanged():
    """The premise. If these ever diverge, the swap below stops being safe."""
    request = make_request(method="GET", path="/", query_string="", headers={}, body=b"")
    # `getattr` rather than `request._state`: the scan below would report this
    # module's own premise as an offence, and writing the private name here
    # only to exempt it would make the exemption the thing to get wrong.
    assert request.state is getattr(request, "_state")


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_a_test_module_uses_the_public_state_property(path: pathlib.Path):
    if path.name == pathlib.Path(__file__).name:
        return
    lines = _private_state_accesses(path)
    assert lines == [], (
        f"{path.name}: `request.state` is the same object and is public - lines {lines}"
    )


def test_a_private_access_would_be_found(tmp_path):
    module = tmp_path / "test_probe.py"
    module.write_text("def test_x():\n    assert request._state\n", encoding="utf-8")
    assert _private_state_accesses(module) == [2]


def test_prose_naming_the_private_field_is_not_an_access(tmp_path):
    """A docstring explaining what the framework writes is not a reach into it."""
    module = tmp_path / "test_probe.py"
    module.write_text(
        'def test_x():\n    """The middleware writes request._state[\'session\']."""\n'
        "    assert True\n",
        encoding="utf-8",
    )
    assert _private_state_accesses(module) == []


def test_the_public_spelling_is_not_an_access(tmp_path):
    module = tmp_path / "test_probe.py"
    module.write_text("def test_x():\n    assert request.state\n", encoding="utf-8")
    assert _private_state_accesses(module) == []
