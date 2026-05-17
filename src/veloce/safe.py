"""Filesystem-safety helpers derived from the OWASP Path Traversal cheatsheet.

`secure_filename(name)` returns a safe basename for a user-supplied filename.
`safe_join(directory, *paths)` joins paths and refuses if the result escapes
the base directory or any component is absolute.

Both helpers are derived from the OWASP guidance and the underlying
filesystem semantics.

References:
- OWASP Path Traversal cheat sheet
- CWE-22 (Improper Limitation of a Pathname to a Restricted Directory)
"""

from __future__ import annotations

import os
import re
import unicodedata

# Permitted characters in a sanitised filename: ASCII letters, digits,
# underscore, period, hyphen. Everything else collapses to underscore.
_VALID_FILENAME_CHAR = re.compile(r"[^A-Za-z0-9_.\-]")

# Windows reserved device names — case-insensitive. Even on POSIX, blocking
# these prevents subtle cross-platform breakage in mounted Windows shares.
_WIN_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def secure_filename(name: str) -> str:
    """Return a safe basename for `name`.

    - Strips directory separators (`/`, `\\`) and any non-ASCII characters.
    - Replaces unsafe characters with underscores; collapses repeats.
    - Strips leading/trailing dots/spaces/underscores (blocks `.` and `..`).
    - Prefixes Windows reserved names (`CON`, `PRN`, …) with `_`.
    - Returns `""` when nothing survives sanitisation.

    Empty or whitespace-only input returns `""`. The caller is responsible
    for treating that as a rejection — `secure_filename` will not raise.
    """
    if not name:
        return ""

    # Normalise unicode and drop everything that doesn't have an ASCII form.
    # NFKD splits accented characters; the encode/decode round-trip discards
    # combining marks and any glyph that can't be represented.
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")

    # Replace path separators explicitly first so traversal segments visibly
    # collapse to underscores in the final name.
    name = name.replace(os.sep, "_").replace("/", "_").replace("\\", "_")

    # Sanitise everything else.
    name = _VALID_FILENAME_CHAR.sub("_", name)

    # Strip surrounds and collapse repeated underscores.
    name = name.strip("._ ")
    name = re.sub(r"_+", "_", name)

    if not name:
        return ""

    # Prefix Windows reserved names so they can never resolve to a device.
    stem = name.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED:
        name = "_" + name

    return name


def safe_join(directory: str, *paths: str) -> str | None:
    """Join `paths` onto `directory`, returning `None` on any escape.

    Returns the absolute joined path if it equals `directory` or is a
    descendant. Returns `None` if:
    - any component in `paths` is an absolute path,
    - any component contains a NUL byte,
    - the resolved path is outside `directory`.

    The check is performed via `os.path.abspath`, which collapses `..`
    segments before comparison. Symlinks are **not** resolved — callers
    that distrust symlinks must use `os.path.realpath` themselves.
    """
    if not directory:
        return None

    base = os.path.abspath(directory)

    for component in paths:
        if component is None:
            return None
        if "\x00" in component:
            return None
        # Reject absolute path components — `os.path.join` would otherwise
        # silently discard `base` if a later argument is absolute.
        if os.path.isabs(component):
            return None

    joined = os.path.abspath(os.path.join(base, *paths))

    # `joined` must be exactly `base`, or under it. Adding the separator
    # avoids `base="/srv/a"` accepting `joined="/srv/abc"` as a child.
    if joined == base:
        return joined
    if joined.startswith(base + os.sep):
        return joined
    return None
