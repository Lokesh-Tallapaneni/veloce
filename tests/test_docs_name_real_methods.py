"""Every `app.method()` the shipped docs name exists on `Veloce`.

`SECURITY.md` told readers to call `app.use_secure_defaults()`, which the same
release removed. The removal is in `CHANGELOG.md`; the security guide was not
updated with it, so the one document a reader consults specifically to harden a
deployment handed them an `AttributeError`.

A grep at the time of removal would have caught it, which is the point: this
makes the grep run on every commit instead of relying on someone remembering to
do it.

Scoped to what actually ships - `README.md`, `SECURITY.md`, and the `docs/` tree
- and to the unambiguous spelling, a backticked `app.name(` or `Veloce.name(`.
Prose that merely mentions a method is not matched, and neither is
`response.encode()` or any other receiver, because this cannot know what those
names refer to.
"""

from __future__ import annotations

import pathlib
import re

from veloce import Veloce

#: A backticked call on the application object. The trailing `(` is what makes
#: it a call rather than a mention, and the receiver is pinned to the two
#: spellings the docs use for the app.
_CALL = re.compile(r"`(?:app|Veloce)\.([a-z_][a-z0-9_]*)\(")

#: What gets published. `internal/` is deliberately absent: it is gitignored
#: working material and is allowed to name things that no longer exist.
_SHIPPED = ("README.md", "SECURITY.md")


def _documents() -> list[pathlib.Path]:
    root = pathlib.Path(__file__).resolve().parent.parent
    paths = [root / name for name in _SHIPPED]
    paths += sorted((root / "docs").rglob("*.md"))
    return [path for path in paths if path.is_file()]


def test_the_scan_finds_something_to_check():
    """The floor: a scan of nothing would pass the test below for free.

    If the pattern stops matching - a docs reformat drops the backticks, the
    tree moves - this fails rather than quietly asserting nothing.
    """
    found = {
        name for path in _documents() for name in _CALL.findall(path.read_text(encoding="utf-8"))
    }

    assert len(found) >= 10, f"only {len(found)} app method calls found in the docs"


def test_every_documented_app_method_exists():
    """The regression: `app.use_secure_defaults()` outlived the method."""
    missing: list[str] = []
    for path in _documents():
        for name in sorted(set(_CALL.findall(path.read_text(encoding="utf-8")))):
            if not hasattr(Veloce, name):
                missing.append(f"{path.name}: app.{name}()")

    assert not missing, (
        "the docs tell a reader to call methods that do not exist - remove them or "
        f"restore the method: {missing}"
    )


def test_the_check_would_catch_a_removed_method():
    """The mutation, inline: a name that does not exist must be reported.

    Without this the test above passes on an empty codebase, a broken pattern,
    or a `hasattr` that always answers True.
    """
    assert not hasattr(Veloce, "use_secure_defaults")
    assert _CALL.findall("call `app.use_secure_defaults()` to harden") == ["use_secure_defaults"]


def test_a_real_method_is_recognised():
    """The other half of the mutation: a name that does exist must pass."""
    assert _CALL.findall("register with `app.add_middleware(...)`") == ["add_middleware"]
    assert hasattr(Veloce, "add_middleware")
