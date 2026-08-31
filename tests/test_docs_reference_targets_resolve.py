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

import veloce

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


# ── and the other direction: every export is named by a page ─────────
#
# The reverse check above is the one that fails the docs build. This one is the
# direction that ships silently: an export with no `:::` directive simply has
# no reference page, and nothing notices. `GZipMiddleware`'s docstring linked to
# `#veloce.CompressionMiddleware`, an anchor mkdocstrings never created because
# no page named that symbol, so the link was dead on the published site.


def _documented_names() -> set[str]:
    """Every `veloce.X` named by a `:::` directive under `docs/reference/`."""
    documented: set[str] = set()
    for page in sorted((DOCS / "reference").rglob("*.md")):
        documented |= {
            match.group(1)
            for match in re.finditer(
                r"^::: veloce\.([A-Za-z_][A-Za-z0-9_]*)$",
                page.read_text(encoding="utf-8"),
                re.M,
            )
        }
    return documented


def test_the_directive_scan_reads_a_real_corpus():
    """A scan of nothing would pass the check below on an empty docs tree."""
    assert len(_documented_names()) > 200


def test_every_top_level_export_has_a_reference_entry():
    """An export with no `:::` directive has no page a reader can reach."""
    undocumented = sorted(name for name in veloce.__all__ if name not in _documented_names())
    assert not undocumented, (
        "these are in `veloce.__all__` but no `docs/reference/` page names them, "
        f"so they render nowhere and cannot be linked to: {undocumented}"
    )
