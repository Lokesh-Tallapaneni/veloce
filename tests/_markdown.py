"""Read the fenced code blocks out of a documentation page.

Written twice, byte for byte apart from whether the page was a parameter or a
module-level constant: once in `test_mcp_guide_claims.py` and once in
`test_database_and_graphql_guides.py`. A framing fix - a tilde fence, an
indented fence, a language tag carrying attributes - had two places to land, and
every guard built on either scan would have kept passing from the other.
"""

from __future__ import annotations

import pathlib


def blocks(page: pathlib.Path) -> list[tuple[int, str, str]]:
    """Every fenced block on `page`, as `(line number, language, source)`.

    The line number is the fence's own, so a failure can be pointed at. A fence
    with no language tag reports `"text"`.
    """
    lines = page.read_text(encoding="utf-8").splitlines()
    found: list[tuple[int, str, str]] = []
    current: list[str] | None = None
    language = "text"
    start = 0
    for number, line in enumerate(lines, 1):
        if line.startswith("```") and current is None:
            language = line[3:].strip() or "text"
            current, start = [], number
        elif line.startswith("```") and current is not None:
            found.append((start, language, "\n".join(current)))
            current = None
        elif current is not None:
            current.append(line)
    return found


def python_blocks(page: pathlib.Path) -> list[tuple[int, str]]:
    """Just the Python blocks, as `(line number, source)`."""
    return [(number, code) for number, language, code in blocks(page) if language == "python"]
