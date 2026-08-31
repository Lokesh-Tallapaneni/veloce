"""The installed version — read from the package metadata that owns it.

`pyproject.toml` is the single source of the version, so it is read back from
the installed distribution rather than restated as a literal here. The read is
not free: it walks the distribution's metadata files, which a cold interpreter
pays for in real time, so callers reach for it lazily.

Both `veloce.__version__` and `veloce --version` resolve through this. They each
carried their own copy before, and the two fallbacks had already drifted apart.
"""

from __future__ import annotations

#: Returned when the metadata is not there to read: an editable install before
#: it has been materialised, or a runtime without importlib.metadata. It is
#: deliberately not a plausible version number - a second hand-maintained
#: literal would drift from `pyproject.toml`, which is what happened before.
UNKNOWN_VERSION = "0.0.0+unknown"


def resolve_version() -> str:
    """Return the installed `veloceframework` version, or the unknown sentinel."""
    # Imported here rather than at module level so that merely importing
    # `veloce` does not pull in importlib.metadata and its dependencies; this
    # runs at most once per process.
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("veloceframework")
    except PackageNotFoundError:
        return UNKNOWN_VERSION
    except Exception:
        # An unsupported or sandboxed runtime where the metadata cannot be read
        # at all. A version string is never worth failing an import over.
        return UNKNOWN_VERSION
