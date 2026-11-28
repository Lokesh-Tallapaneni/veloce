"""A public function takes its boolean options by keyword.

`f(path, True, False)` says nothing at the call site and silently changes
meaning if parameters are reordered, so a boolean parameter on a public
function or method sits after a bare `*`. Private helpers (an underscore in the
module, class or function name) keep positional booleans: they are called from
the per-request path, where a keyword call costs more than a positional one and
the reader is the framework's own maintainer.

`KNOWN_POSITIONAL` records the public signatures that predate the rule and keep
accepting positional booleans for compatibility. It may shrink; a new entry
needs a reason.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce"

#: `module.Class.function` or `module.function` -> why it stays positional.
KNOWN_POSITIONAL: dict[str, str] = {
    "encoders.jsonable_encoder": (
        "positional filters are part of its documented call shape and the recursion "
        "inside it passes them positionally on the response-serialisation path"
    ),
}


def _is_boolean(arg: ast.arg, default: ast.expr | None) -> bool:
    if isinstance(arg.annotation, ast.Name) and arg.annotation.id == "bool":
        return True
    return isinstance(default, ast.Constant) and isinstance(default.value, bool)


def _public_defs():
    for path in sorted(SRC.rglob("*.py")):
        parts = path.relative_to(SRC).with_suffix("").parts
        if any(p.startswith("_") and p != "__init__" for p in parts):
            continue
        module = ".".join(p for p in parts if p != "__init__")
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node: ast.AST, scope: tuple[str, ...]):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    if not child.name.startswith("_"):
                        yield from walk(child, (*scope, child.name))
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # A property setter's one parameter is the assigned value.
                    is_setter = any(
                        isinstance(d, ast.Attribute) and d.attr == "setter"
                        for d in child.decorator_list
                    )
                    if not is_setter and (
                        not child.name.startswith("_") or child.name == "__init__"
                    ):
                        yield module, scope, child

        yield from walk(tree, ())


def _positional_booleans(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = [*fn.args.posonlyargs, *fn.args.args]
    defaults = [None] * (len(args) - len(fn.args.defaults)) + list(fn.args.defaults)
    return [a.arg for a, d in zip(args, defaults, strict=True) if _is_boolean(a, d)]


def test_public_booleans_are_keyword_only():
    offenders = []
    for module, scope, fn in _public_defs():
        key = ".".join([module, *scope, fn.name])
        names = _positional_booleans(fn)
        if names and key not in KNOWN_POSITIONAL:
            offenders.append(f"{key}: {', '.join(names)}")
    assert offenders == [], (
        "public functions taking a positional boolean (put them after `*`):\n"
        + "\n".join(offenders)
    )


def test_the_scan_sees_public_functions():
    assert sum(1 for _ in _public_defs()) > 500


def test_every_known_exception_still_exists_and_still_needs_it():
    live = {".".join([m, *s, f.name]): _positional_booleans(f) for m, s, f in _public_defs()}
    stale = [key for key in KNOWN_POSITIONAL if not live.get(key)]
    assert stale == [], f"no longer positional (or gone) - remove from KNOWN_POSITIONAL: {stale}"
