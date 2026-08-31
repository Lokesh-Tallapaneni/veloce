"""Where `src/veloce` is, resolved once and asserted non-empty.

Twenty-five modules located the tree themselves, in four spellings. Two of them
matter and disagree: `pathlib.Path(veloce.__file__).parent` follows the imported
package wherever it lives, while `parents[1]/"src"/"veloce"` is the repo layout
and does not exist under a non-editable install - where a scan built on it reads
an empty tree and every guard over it passes having checked nothing.
`tests/_mcp_source.py` documents that failure for the MCP subtree and guards
against it; the same reasoning applies to the whole package.

Resolved through the repo layout when it is there, through the imported package
otherwise, and asserted non-empty either way, so a wrong root fails loudly at
import rather than silently at rest.
"""

from __future__ import annotations

import pathlib

import veloce

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce"

if not SRC.is_dir():  # pragma: no cover - only on a non-editable install
    SRC = pathlib.Path(veloce.__file__).parent

assert len(list(SRC.rglob("*.py"))) > 50, f"no veloce source found under {SRC}"
