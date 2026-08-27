"""Read the shipped MCP source as a tree, not as text.

Five tests asserted on the *text* of `veloce/contrib/mcp/**`, two of them on
the absence of an exactly-formatted line - `"_HANDSHAKE_ONLY_METHODS =
frozenset(\n    {\n" not in source`. That is the most brittle assertion shape
available: `ruff format` deciding to put the brace elsewhere breaks a green
suite while the invariant holds, and the same invariant restated with different
spacing walks straight past it.

Every scan here works on the parsed module, so a rename, a reflow or a line
wrap is invisible and only a change to what the code *does* is visible. It is
the same rule the import-graph and naming guards in this suite already follow.
"""

from __future__ import annotations

import ast
import pathlib

MCP_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce" / "contrib" / "mcp"


def tree(*parts: str) -> ast.Module:
    """The parsed module at `MCP_ROOT/parts`."""
    path = MCP_ROOT.joinpath(*parts)
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def module_level_names(module: ast.Module) -> set[str]:
    """Every name bound by a module-level assignment."""
    names: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def defines(module: ast.Module, name: str) -> bool:
    """True when `module` defines a function called `name` at any depth."""
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in ast.walk(module)
    )


def calls(module: ast.Module, name: str) -> list[int]:
    """Line numbers where `module` calls `name`, bare or as an attribute."""
    found = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id == name) or (
            isinstance(func, ast.Attribute) and func.attr == name
        ):
            found.append(node.lineno)
    return found


def attribute_chains(module: ast.Module, *chain: str) -> list[int]:
    """Line numbers of an attribute access ending in `chain`, e.g. marker.description."""
    found = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Attribute):
            continue
        parts = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if parts[: len(chain)] == list(reversed(chain)):
            found.append(node.lineno)
    return found


def dict_values_for_key(module: ast.Module, key: str) -> set[str]:
    """Names assigned to `key` in every dict literal in `module`."""
    values: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Dict):
            continue
        for entry_key, entry_value in zip(node.keys, node.values):
            if (
                isinstance(entry_key, ast.Constant)
                and entry_key.value == key
                and isinstance(entry_value, ast.Name)
            ):
                values.add(entry_value.id)
    return values
