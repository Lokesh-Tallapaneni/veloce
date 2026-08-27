"""No module-level import cycle inside the package.

`datastructures.py` and `formparsers.py` were the package's only genuine one.
`formparsers` imports `FormData`, `Headers` and `UploadFile` from
`datastructures` - the honest direction, a parser needing the containers it
fills. `datastructures` then imported `formparsers` back, as the **last
statement in the file** under `# noqa: E402`, purely to re-export
`parse_multipart_form`.

That position is what made it survivable: by the time the last line runs,
everything `formparsers` needs from `datastructures` is defined. Move the
import up, or add one class below it, and the package stops importing. A cycle
that works only because of where a line sits is not a resolved cycle.

It existed because `request.py` reached the parser *through* the shim, so the
back-edge had a live consumer inside the package. It imports from
`formparsers` directly now, and `veloce.http.__init__` - the gateway that
actually defines the public surface - already exported the name. The shim is
gone.

The scan is module-level imports only. A deferred import inside a function is a
different thing with its own guard in `test_deferred_import_discipline.py`.

It also excludes edges into the top-level `veloce` gateway. A submodule doing
`from veloce import Request` closes a loop with `veloce/__init__.py`, and 22
such loops exist - but that is the package-gateway pattern, not a defect: the
gateway imports its submodules in a deliberate order and every name is bound
before a submodule reads it. Unwinding it means every submodule importing from
leaf paths, which is a different change with a different justification.

**With those excluded the graph has zero cycles**, which is what makes this a
guard rather than a wish: the one the finding named was the only leaf-to-leaf
cycle in the package.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce"


def _module_name(path: pathlib.Path) -> str:
    parts = path.relative_to(ROOT.parent).with_suffix("").parts
    return ".".join(parts[:-1]) if parts[-1] == "__init__" else ".".join(parts)


def _module_level_imports(path: pathlib.Path) -> set[str]:
    """The `veloce.*` modules this one imports at module scope."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("veloce"):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("veloce"))
        elif isinstance(node, ast.If):
            # `if TYPE_CHECKING:` blocks do not import at runtime.
            continue
    return found


#: A submodule importing from the package gateway closes a loop with it by
#: construction. See the module docstring.
GATEWAY = "veloce"


def _graph(*, include_gateway: bool = False) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in ROOT.rglob("*.py"):
        edges = _module_level_imports(path)
        if not include_gateway:
            edges = {e for e in edges if e != GATEWAY}
        graph[_module_name(path)] = edges
    return graph


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Every cycle reachable in the module-level import graph."""
    found: list[list[str]] = []
    seen: set[str] = set()

    def walk(node: str, stack: list[str], on_stack: set[str]) -> None:
        for target in sorted(graph.get(node, ())):
            if target not in graph:
                continue
            if target in on_stack:
                found.append([*stack[stack.index(target) :], target])
                continue
            if (node, target) in seen:
                continue
            seen.add((node, target))
            walk(target, [*stack, target], on_stack | {target})

    for start in sorted(graph):
        walk(start, [start], {start})
    return found


# ── the graph is acyclic ─────────────────────────────────────────────


def test_no_module_level_import_cycle():
    cycles = _cycles(_graph())
    assert cycles == [], "module-level import cycles: " + "; ".join(" -> ".join(c) for c in cycles)


def test_datastructures_does_not_import_formparsers():
    """The specific back-edge, named, so a re-added shim fails on its own."""
    assert "veloce.http.formparsers" not in _module_level_imports(
        ROOT / "http" / "datastructures.py"
    )


def test_formparsers_still_imports_datastructures():
    """The forward edge is correct and must stay - a parser needs its containers."""
    assert "veloce.http.datastructures" in _module_level_imports(ROOT / "http" / "formparsers.py")


def test_request_imports_the_parser_from_its_own_module():
    """Not through a re-export, which is what kept the back-edge alive."""
    assert "veloce.http.formparsers" in _module_level_imports(ROOT / "http" / "request.py")


# ── the scan is not vacuous ──────────────────────────────────────────


def test_the_gateway_pattern_is_what_is_excluded():
    """Named, so the exclusion cannot quietly widen: with gateway edges counted
    the graph has cycles, and every one of them runs through `veloce`."""
    cycles = _cycles(_graph(include_gateway=True))
    assert cycles, "the exclusion is doing nothing"
    assert all(GATEWAY in cycle for cycle in cycles)


def test_the_graph_covers_the_package():
    graph = _graph()
    assert len(graph) > 100
    assert "veloce.http.request" in graph
    assert graph["veloce.http.formparsers"]


def test_a_cycle_would_be_found():
    assert _cycles({"a": {"b"}, "b": {"a"}}) != []
    assert _cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}}) != []


def test_an_acyclic_graph_is_clean():
    assert _cycles({"a": {"b"}, "b": {"c"}, "c": set()}) == []


def test_a_self_import_would_be_found():
    assert _cycles({"a": {"a"}}) != []


# ── and the public name is still reachable ───────────────────────────


def test_the_parser_is_exported_from_the_gateway():
    """Removing the shim must not remove the name from the documented surface."""
    import veloce.http

    assert "parse_multipart_form" in veloce.http.__all__
    assert veloce.http.parse_multipart_form is not None


@pytest.mark.parametrize(
    "name",
    ["DEFAULT_MAX_MULTIPART_PARTS", "DEFAULT_MAX_MULTIPART_PART_SIZE", "parse_multipart_form"],
)
def test_the_re_exported_names_live_in_formparsers(name):
    import veloce.http.formparsers as formparsers

    assert hasattr(formparsers, name)
