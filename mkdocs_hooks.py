"""MkDocs build hooks - inject the package version into the docs config.

Sources the version from pyproject.toml (the single source of truth) so the
docs header version never drifts from the published package version.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def on_config(config: Any, **kwargs: Any) -> Any:
    pyproject = Path(config.config_file_path).parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if match:
        extra = config.setdefault("extra", {})
        extra["version"] = match.group(1)
    return config
