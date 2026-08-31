"""The bridge marker binds its reader to its writers through one name.

`instrument_with_otel` refuses a second registration by looking for an
attribute on the already-registered hook. Read through `_BRIDGE_MARKER` and
written as a literal, the constant names nothing: renaming it leaves the guard
looking for an attribute nobody sets, and the second registration succeeds -
every request then carries two spans and the duplicate is silent.
"""

from __future__ import annotations

import ast
import pathlib

OTEL = pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce" / "otel.py"


def test_the_marker_literal_appears_exactly_once() -> None:
    source = OTEL.read_text(encoding="utf-8")
    assert source.count('"_veloce_otel_bridge"') == 1, (
        "the marker string is spelled more than once in otel.py; every writer "
        "and the guard must go through _BRIDGE_MARKER"
    )
    assert "_veloce_otel_bridge = True" not in source, (
        "a writer sets the marker as a literal attribute; use "
        "setattr(fn, _BRIDGE_MARKER, True) so a rename reaches it"
    )


def test_both_writers_and_the_guard_reference_the_constant() -> None:
    tree = ast.parse(OTEL.read_text(encoding="utf-8"))
    uses = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "_BRIDGE_MARKER"
    ]
    # One binding, one `getattr` read, two `setattr` writes.
    assert len(uses) == 4, f"expected 4 references to _BRIDGE_MARKER, found {len(uses)}"


def test_a_second_registration_is_still_refused() -> None:
    """The behaviour the marker exists for, not just its spelling."""
    import pytest

    pytest.importorskip("opentelemetry")
    from veloce import Veloce
    from veloce.otel import instrument_with_otel

    app = Veloce(openapi_url=None)
    first = instrument_with_otel(app)
    with pytest.warns(RuntimeWarning, match="already called"):
        second = instrument_with_otel(app)
    assert second is first
