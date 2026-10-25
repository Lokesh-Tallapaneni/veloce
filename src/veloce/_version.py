"""The installed version — read from the package metadata that owns it.

`pyproject.toml` is the single source of the version, so it is read back from the
installed distribution rather than restated as a literal here. The read walks the
distribution's metadata files, so callers reach for it lazily.
"""

from __future__ import annotations

#: Returned when the metadata cannot be read. Deliberately not a plausible
#: version number: a second hand-maintained literal would drift.
UNKNOWN_VERSION = "0.0.0+unknown"


def resolve_version() -> str:
    """Return the installed `veloceframework` version, or the unknown sentinel."""
    # Imported here so that importing `veloce` does not pull in importlib.metadata.
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("veloceframework")
    except PackageNotFoundError:
        return UNKNOWN_VERSION
    except Exception:
        # A version string is never worth failing an import over.
        return UNKNOWN_VERSION
