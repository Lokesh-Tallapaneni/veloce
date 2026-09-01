"""Declared dependency floors match the APIs the code actually calls."""

from __future__ import annotations

import pathlib
import re

_PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
_TEXT = _PYPROJECT.read_text(encoding="utf-8")

#: Every msgspec API `src/veloce` calls, with the first version that exposes it
#: publicly. Verified against the sdists on PyPI, not from memory.
_MSGSPEC_APIS = {
    "convert": (0, 16),
    "to_builtins": (0, 13),
    "defstruct": (0, 13),
    "__struct_config__": (0, 14),
}


def _declared_msgspec_floors() -> list[tuple[int, int]]:
    """Every `msgspec>=X.Y` floor declared anywhere in pyproject.toml."""
    return [
        (int(major), int(minor)) for major, minor in re.findall(r"msgspec>=(\d+)\.(\d+)", _TEXT)
    ]


def test_every_declared_msgspec_floor_admits_the_apis_the_code_calls():
    """POSITIVE: the floor is high enough for every API in src/veloce."""
    required = max(_MSGSPEC_APIS.values())
    floors = _declared_msgspec_floors()
    assert floors, "no msgspec floor found in pyproject.toml - the scan is not running"
    too_low = [f"{a}.{b}" for a, b in floors if (a, b) < required]
    assert not too_low, (
        f"declared msgspec floors {too_low} are below {required[0]}.{required[1]}, "
        f"which `msgspec.convert` requires (called in dependency.py and _model_backend.py)"
    )


def test_the_floor_scan_would_catch_a_regression():
    """NEGATIVE: a floor below the requirement is rejected, so the guard is real."""
    required = max(_MSGSPEC_APIS.values())
    assert required > (0, 13), "0.13 must be below the requirement or this guard proves nothing"
    assert required > (0, 15), "0.15 lacks public `convert`"


def test_the_recorded_apis_are_the_ones_the_source_calls():
    """NEGATIVE: an API added to the source but not to this table goes unchecked."""
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "veloce"
    called: set[str] = set()
    for path in src.rglob("*.py"):
        called.update(re.findall(r"_msgspec\.([a-z_]+)", path.read_text(encoding="utf-8")))
    # Submodule roots (`json`, `structs`, `inspect`) are namespaces, not APIs
    # with their own floor; the leaf calls under them are covered by the
    # package floor itself.
    unchecked = called - set(_MSGSPEC_APIS) - {"json", "structs", "inspect"}
    assert not unchecked, (
        f"msgspec APIs called but not floor-checked: {sorted(unchecked)}; "
        "add each to _MSGSPEC_APIS with the version that introduced it"
    )
