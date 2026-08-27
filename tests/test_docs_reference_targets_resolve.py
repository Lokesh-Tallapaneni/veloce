"""Every `:::` target in the docs names something that still exists.

`mkdocstrings` collects each `::: dotted.path` in `docs/reference/**` at build
time, so a symbol that moves leaves the page naming a path that no longer
resolves and `mkdocs build --strict` aborts. That happened: moving the Swagger
UI and ReDoc host out of the OpenAPI generator left the reference pointing at
`veloce.contrib.openapi.setup_openapi_routes`.

The docs CI job runs on `src/veloce/**` too, so it would have caught it - this
is the same check where a maintainer can run it. `mkdocs build --strict` needs
a native Cairo library for the social-card plugin, which is not installable
everywhere; importing a module is.
"""

from __future__ import annotations

import importlib
import pathlib
import re

import pytest

DOCS = pathlib.Path(__file__).resolve().parents[1] / "docs"
TARGET = re.compile(r"^:::\s+([A-Za-z_][\w.]*)\s*$", re.M)


def _targets() -> list[tuple[str, str]]:
    """Every `::: dotted.path` in the docs tree, with the page that names it."""
    found = []
    for page in sorted(DOCS.rglob("*.md")):
        for path in TARGET.findall(page.read_text(encoding="utf-8")):
            found.append((page.relative_to(DOCS).as_posix(), path))
    return found


TARGETS = _targets()


def test_the_scan_finds_the_reference_pages():
    """A scan matching nothing would make every case below vanish."""
    assert len(TARGETS) > 100, f"only {len(TARGETS)} `:::` targets found"


@pytest.mark.parametrize(
    ("page", "path"), TARGETS, ids=[f"{page}:{path}" for page, path in TARGETS]
)
def test_a_documented_symbol_still_exists(page: str, path: str):
    """Resolve the longest importable prefix, then walk the rest as attributes."""
    parts = path.split(".")
    module = None
    for split in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        rest = parts[split:]
        break
    assert module is not None, f"{page} documents `{path}`, whose package does not import"

    target = module
    for attr in rest:
        assert hasattr(target, attr), (
            f"{page} documents `{path}`, but `{attr}` is not on "
            f"`{getattr(target, '__name__', target)}` - it moved or was removed, "
            "and `mkdocs build --strict` will abort on this page"
        )
        target = getattr(target, attr)
