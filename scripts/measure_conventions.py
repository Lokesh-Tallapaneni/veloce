"""Measure the codebase conventions the project tracks, from the source tree.

Run it from the repository root and compare the numbers between commits:

    uv run python scripts/measure_conventions.py [path/to/another/checkout]

With a path, it measures that checkout instead, so a base commit's worktree can
be compared against the current tree.

It parses every module under `src/veloce` with `ast` and reports size,
documentation, annotation and import-cost figures. Nothing is imported except
in the final import-time probe, which runs in a fresh interpreter.
"""

from __future__ import annotations

import ast
import os
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "veloce"
BUDGET = 800


def _public(name: str) -> bool:
    return not name.startswith("_")


def _percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def _walk(tree: ast.Module):
    """Yield `(node, is_public)` for every class and function reachable publicly."""

    def visit(node: ast.AST, public: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                is_public = public and _public(child.name)
                yield child, is_public
                yield from visit(child, is_public)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child, public and (_public(child.name) or child.name == "__init__")

    yield from visit(tree, True)


def _documented_parameters(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
    """Return `(documented, total)` public parameters, via `Doc()` or an Args block."""
    args = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
    names = [a for a in args if a.arg not in ("self", "cls")]
    docstring = ast.get_docstring(fn) or ""
    documented = 0
    for a in names:
        in_doc = isinstance(a.annotation, ast.Subscript) and "Doc(" in ast.unparse(a.annotation)
        in_args = f"{a.arg}:" in docstring or f"{a.arg} (" in docstring
        documented += bool(in_doc or in_args)
    return documented, len(names)


def main() -> None:
    files = sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)
    sizes = [len(p.read_text(encoding="utf-8").splitlines()) for p in files]

    classes = documented_classes = 0
    functions = documented_functions = 0
    parameters = documented_parameters = 0
    with_sections = 0
    module_vars = annotated_vars = 0
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, public in _walk(tree):
            if not public:
                continue
            if isinstance(node, ast.ClassDef):
                classes += 1
                documented_classes += bool(ast.get_docstring(node))
            else:
                functions += 1
                documented_functions += bool(ast.get_docstring(node))
                done, total = _documented_parameters(node)
                documented_parameters += done
                parameters += total
                doc = ast.get_docstring(node) or ""
                with_sections += any(s in doc for s in ("Args:", "Returns:", "Raises:"))
        for stmt in tree.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                module_vars += 1
                annotated_vars += 1
            elif isinstance(stmt, ast.Assign) and all(
                isinstance(t, ast.Name) for t in stmt.targets
            ):
                module_vars += 1

    over = sorted(
        (
            (n, p.relative_to(SRC).as_posix())
            for n, p in zip(sizes, files, strict=True)
            if n > BUDGET
        ),
        reverse=True,
    )
    print(f"source files                       {len(files)}")
    print(f"source lines                       {sum(sizes)}")
    print(f"lines per file (median / max)      {statistics.median(sizes):.0f} / {max(sizes)}")
    print(f"files over {BUDGET} lines                {len(over)}")
    for n, rel in over:
        print(f"    {rel}: {n}")
    print(f"public classes documented          {_percent(documented_classes, classes)}")
    print(f"public functions documented        {_percent(documented_functions, functions)}")
    print(f"public functions with sections     {_percent(with_sections, functions)}")
    print(f"public parameters documented       {_percent(documented_parameters, parameters)}")
    print(f"module variables annotated         {_percent(annotated_vars, module_vars)}")

    probe = (
        "import sys, time\n"
        "t = time.perf_counter()\n"
        "import veloce\n"
        "ms = (time.perf_counter() - t) * 1000\n"
        "n = len([m for m in sys.modules if m == 'veloce' or m.startswith('veloce.')])\n"
        "print(f'{ms:.1f} ms, {n} veloce modules')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, cwd=ROOT
    )
    print(f"import veloce                      {result.stdout.strip() or result.stderr.strip()}")


if __name__ == "__main__":
    main()
