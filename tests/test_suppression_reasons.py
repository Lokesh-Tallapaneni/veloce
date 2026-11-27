"""Every lint or type-checker suppression in the package states its reason.

A bare ``# noqa: X`` or ``# type: ignore[x]`` tells the next reader that a rule
was silenced but not why, so it cannot be judged stale or safe to remove. The
reason follows the rule code: ``# noqa: BLE001 - logged and restarted`` or
``# type: ignore[attr-defined]  # set on the generated function``.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "veloce"

_NOQA = re.compile(r"#\s*noqa:\s*[A-Z0-9, ]+?(?:\s+[-—]\s+(?P<why>\S.*))?$")
_IGNORE = re.compile(r"#\s*type:\s*ignore(?:\[[^\]]*\])?(?P<rest>.*)$")


def _comments(path: Path):
    source = path.read_text(encoding="utf-8")
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            yield tok.start[0], tok.string


def test_every_suppression_carries_a_reason():
    bare: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for line, text in _comments(path):
            noqa = _NOQA.search(text)
            if noqa and not noqa.group("why"):
                bare.append(f"{path.relative_to(SRC)}:{line}: {text}")
                continue
            ignore = _IGNORE.search(text)
            if ignore and "#" not in ignore.group("rest"):
                bare.append(f"{path.relative_to(SRC)}:{line}: {text}")
    assert bare == [], "suppressions without a stated reason:\n" + "\n".join(bare)
