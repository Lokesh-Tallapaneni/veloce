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


def function(module: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The function called `name` at any depth in `module`, or `None`.

    Lets every scan below be applied to one function rather than a whole module,
    which is what a source-text slice (`source[source.index("async def x"):]`)
    was standing in for.
    """
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def assigned_names(node: ast.AST) -> set[str]:
    """Every name bound by an assignment inside `node`, tuple targets included."""
    names: set[str] = set()
    for child in ast.walk(node):
        targets: list[ast.expr] = []
        if isinstance(child, ast.Assign):
            targets = list(child.targets)
        elif isinstance(child, (ast.AnnAssign, ast.AugAssign)):
            targets = [child.target]
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return names


def assigned_call_names(node: ast.AST, target: str) -> set[str]:
    """Names called to produce the value assigned to `target`.

    An assignment whose value is not a call contributes nothing, so an empty
    result means `target` is computed some other way - which is exactly the
    distinction a test pinning `is_modern = is_modern_version(...)` is making.
    """
    called: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == target for t in child.targets):
            continue
        if not isinstance(child.value, ast.Call):
            continue
        func = child.value.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute):
            called.add(func.attr)
    return called


def membership_tests(node: ast.AST, needle: str, *, negated: bool = False) -> set[str]:
    """Names tested as ``needle in <name>`` (or ``not in`` when `negated`)."""
    wanted = ast.NotIn if negated else ast.In
    found: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Compare) or len(child.ops) != 1:
            continue
        if not isinstance(child.ops[0], wanted):
            continue
        if not (isinstance(child.left, ast.Constant) and child.left.value == needle):
            continue
        for comparator in child.comparators:
            if isinstance(comparator, ast.Name):
                found.add(comparator.id)
    return found


def call_args(node: ast.AST, name: str) -> list[tuple[str, ...]]:
    """The positional argument spellings of every call to `name`.

    A `Name` argument contributes its identifier and anything else contributes
    `"<expr>"`, so a test can say "called with `payload`" without pinning the
    formatting of the call.
    """
    shapes: list[tuple[str, ...]] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        matched = (isinstance(func, ast.Name) and func.id == name) or (
            isinstance(func, ast.Attribute) and func.attr == name
        )
        if matched:
            shapes.append(tuple(a.id if isinstance(a, ast.Name) else "<expr>" for a in child.args))
    return shapes


def compared_constants(node: ast.AST, name: str) -> set[object]:
    """Constants `name` is compared against with `==` anywhere in `node`.

    The AST form of scanning a dispatch chain for its cases. A regex over the
    source (``if grant_type == "([a-z_]+)"``) finds only the branches written in
    that one spelling, so a reflow or an `elif` chain silently narrows what the
    test believes the code dispatches on.
    """
    found: set[object] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Compare) or len(child.ops) != 1:
            continue
        if not isinstance(child.ops[0], ast.Eq):
            continue
        if not (isinstance(child.left, ast.Name) and child.left.id == name):
            continue
        for comparator in child.comparators:
            if isinstance(comparator, ast.Constant):
                found.add(comparator.value)
    return found
