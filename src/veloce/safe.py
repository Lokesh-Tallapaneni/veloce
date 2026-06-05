"""Safety helpers — constant-time comparison, filename sanitisation, path joining.

`constant_time_compare(a, b)` compares two secrets without timing leaks.
`secure_filename(name)` returns a safe basename for a user-supplied filename.
`safe_join(directory, *paths)` joins paths and refuses if the result escapes
the base directory or any component is absolute.

The filesystem helpers are derived from the OWASP guidance and the underlying
filesystem semantics.

References:
- OWASP Path Traversal cheat sheet
- CWE-22 (Improper Limitation of a Pathname to a Restricted Directory)
"""

from __future__ import annotations

import hmac
import os
import re
import unicodedata

# Permitted characters in a sanitised filename: ASCII letters, digits,
# underscore, period, hyphen. Everything else collapses to underscore.
_VALID_FILENAME_CHAR = re.compile(r"[^A-Za-z0-9_.\-]")
# Run-of-underscores collapser - kept module-level so the compile cost
# is paid once instead of every `re.sub` cache lookup.
_UNDERSCORE_RUN = re.compile(r"_+")

# Windows reserved device names - case-insensitive. Even on POSIX, blocking
# these in `secure_filename` prevents subtle cross-platform breakage in
# mounted Windows shares. `CONIN$`/`CONOUT$` are the console device aliases.
_WIN_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

# `safe_join`'s device-name guard only matters on Windows, where opening a
# path whose segment names a device (e.g. `static/COM1`) can hang a worker
# at `os.stat`. Computed once so POSIX deployments pay no per-request cost.
_IS_NT = os.name == "nt"


def _is_reserved_device(segment: str) -> bool:
    """Whether a single path segment names a Windows reserved device.

    Windows resolves a device by the name's stem, ignoring any extension and
    trailing dots/spaces - so `COM1`, `COM1.txt`, `COM1.` and `COM1 ` all hit
    the `COM1` device. A longer name that merely starts with a device token
    (`COM10`, `CONfig`) is a normal file and is not matched.
    """
    stem = segment.partition(".")[0].rstrip(" .").upper()
    return stem in _WIN_RESERVED


def constant_time_compare(a: str | bytes, b: str | bytes) -> bool:
    """Compare two secrets without leaking their contents through timing.

    Wraps ``hmac.compare_digest``; ``str`` inputs are UTF-8 encoded first.
    """
    if isinstance(a, str) and isinstance(b, str):
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    if isinstance(a, (bytes, bytearray)) and isinstance(b, (bytes, bytearray)):
        return hmac.compare_digest(bytes(a), bytes(b))
    return False


def secure_filename(name: str) -> str:
    """Return a safe basename for `name`.

    - Strips directory separators (`/`, `\\`) and any non-ASCII characters.
    - Replaces unsafe characters with underscores; collapses repeats.
    - Strips leading/trailing dots/spaces/underscores (blocks `.` and `..`).
    - Prefixes Windows reserved names (`CON`, `PRN`, ...) with `_`.
    - Returns `""` when nothing survives sanitisation.

    Empty or whitespace-only input returns `""`. The caller is responsible
    for treating that as a rejection - `secure_filename` will not raise.
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
    name = _UNDERSCORE_RUN.sub("_", name)

    if not name:
        return ""

    # Prefix Windows reserved names so they can never resolve to a device.
    if _is_reserved_device(name):
        name = "_" + name

    return name


def safe_join(directory: str, *paths: str) -> str | None:
    """Join `paths` onto `directory`, returning `None` on any escape.

    Returns the absolute joined path if it equals `directory` or is a
    descendant. Returns `None` if:
    - any component in `paths` is an absolute path,
    - any component contains a NUL byte,
    - on Windows, any segment names a reserved device (`COM1`, `NUL`, ...),
    - the resolved path is outside `directory`.

    The check is performed via `os.path.abspath`, which collapses `..`
    segments before comparison. Symlinks are **not** resolved - callers
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
        # Reject absolute path components - `os.path.join` would otherwise
        # silently discard `base` if a later argument is absolute.
        if os.path.isabs(component):
            return None
        # On Windows, refuse any segment that names a reserved device, so a
        # request like `static/COM1` can never reach `os.stat` and hang.
        if _IS_NT:
            for segment in component.replace("/", os.sep).split(os.sep):
                if segment and _is_reserved_device(segment):
                    return None

    joined = os.path.abspath(os.path.join(base, *paths))

    # Compare via `normcase` so Windows drive-letter casing and separator
    # variants don't cause a same-directory descendant to be rejected.
    # On POSIX `normcase` is the identity, so this is a no-op there.
    base_cmp = os.path.normcase(base)
    joined_cmp = os.path.normcase(joined)

    # When `base` is the filesystem root ("/" on POSIX, "C:\\" on Windows)
    # it already ends with `os.sep`; appending another would produce "//"
    # / "C:\\\\" and never match a legitimate descendant.
    prefix = base_cmp if base_cmp.endswith(os.sep) else base_cmp + os.sep

    # `joined` must be exactly `base`, or under it. Using `prefix` instead
    # of a bare string concat avoids `base="/srv/a"` accepting
    # `joined="/srv/abc"` as a child.
    if joined_cmp == base_cmp:
        return joined
    if joined_cmp.startswith(prefix):
        return joined
    return None
