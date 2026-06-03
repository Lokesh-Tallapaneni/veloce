"""secure_filename + safe_join + constant_time_compare tests (SEC11, SEC12, S8)."""

from __future__ import annotations

import os
import sys

import pytest

from veloce.safe import constant_time_compare, safe_join, secure_filename

# ── constant_time_compare (S8) ───────────────────────────────────────


def test_constant_time_compare_equal_strings():
    assert constant_time_compare("s3cr3t-token", "s3cr3t-token") is True


def test_constant_time_compare_unequal_strings():
    assert constant_time_compare("s3cr3t-token", "wrong-token!!") is False


def test_constant_time_compare_equal_bytes():
    assert constant_time_compare(b"\x00\x01key", b"\x00\x01key") is True


def test_constant_time_compare_type_mismatch_is_false():
    """str vs bytes is a plain non-match, not an exception."""
    assert constant_time_compare("token", b"token") is False


def test_constant_time_compare_exported_from_package():
    from veloce import constant_time_compare as exported

    assert exported is constant_time_compare


# ── secure_filename ──────────────────────────────────────────────────


def test_secure_filename_basic_alnum_underscored():
    assert secure_filename("My cool file.txt") == "My_cool_file.txt"


def test_secure_filename_strips_path_separators():
    assert secure_filename("../etc/passwd") == "etc_passwd"
    assert secure_filename("foo/bar/baz") == "foo_bar_baz"
    assert secure_filename("foo\\bar\\baz") == "foo_bar_baz"


def test_secure_filename_unicode_normalised_to_ascii():
    # Accented characters: NFKD splits them; the combining marks fall away.
    assert secure_filename("résumé.pdf") == "resume.pdf"
    # CJK characters have no ASCII form → only the extension survives, and
    # the leading dot is stripped so the result is the bare stem-suffix.
    assert secure_filename("文書.txt") == "txt"


def test_secure_filename_collapses_repeated_underscores():
    # Spaces become underscores; repeated underscores collapse to one.
    assert secure_filename("a   b   c") == "a_b_c"
    # Multiple separators that all become `_`:
    assert secure_filename("a//b\\\\c") == "a_b_c"
    # Hyphens and dots are preserved literally — only `_` collapses.
    assert secure_filename("a-b") == "a-b"


def test_secure_filename_strips_leading_trailing_dots_spaces():
    assert secure_filename(" . . . hi . . . ") == "hi"
    assert secure_filename("...") == ""
    assert secure_filename("..") == ""
    assert secure_filename(".") == ""


def test_secure_filename_empty_returns_empty():
    assert secure_filename("") == ""
    assert secure_filename("   ") == ""
    assert secure_filename("/") == ""


def test_secure_filename_blocks_windows_reserved_names():
    # Windows reserved device names get an underscore prefix so they
    # can never address a device.
    assert secure_filename("CON") == "_CON"
    assert secure_filename("PRN") == "_PRN"
    assert secure_filename("AUX") == "_AUX"
    assert secure_filename("NUL") == "_NUL"
    assert secure_filename("COM1") == "_COM1"
    assert secure_filename("LPT9") == "_LPT9"
    # Reserved name with an extension still gets prefixed.
    assert secure_filename("CON.txt") == "_CON.txt"
    # Case-insensitive.
    assert secure_filename("con") == "_con"
    # `secure_filename` sanitises `$` to `_` and strips the trailing `_`,
    # so the console aliases never reach the device check here - they are
    # covered by `safe_join` below, which inspects raw segments.
    assert secure_filename("CONIN$") == "CONIN"


def test_secure_filename_preserves_hyphen_underscore_dot():
    assert secure_filename("my-file_v2.tar.gz") == "my-file_v2.tar.gz"


# ── safe_join ─────────────────────────────────────────────────────────


def test_safe_join_simple():
    base = os.path.abspath("/srv/uploads")
    result = safe_join("/srv/uploads", "file.txt")
    assert result == os.path.join(base, "file.txt")


def test_safe_join_subdir():
    base = os.path.abspath("/srv/uploads")
    result = safe_join("/srv/uploads", "sub", "file.txt")
    assert result == os.path.join(base, "sub", "file.txt")


def test_safe_join_rejects_dotdot_escape():
    assert safe_join("/srv/uploads", "../etc/passwd") is None
    assert safe_join("/srv/uploads", "../../etc/passwd") is None
    assert safe_join("/srv/uploads", "sub/../../etc/passwd") is None


def test_safe_join_accepts_dotdot_that_stays_in_base():
    base = os.path.abspath("/srv/uploads")
    # `..` that doesn't escape is permitted (file in same directory).
    assert safe_join("/srv/uploads", "sub/../file.txt") == os.path.join(base, "file.txt")


def test_safe_join_rejects_absolute_path_component():
    # Even if the absolute path happens to be inside `base`, reject it —
    # an absolute path in user input is almost always an attack signal,
    # and os.path.join silently discards `base` when given one.
    assert safe_join("/srv/uploads", "/etc/passwd") is None
    if os.name == "nt":
        assert safe_join("C:\\srv\\uploads", "C:\\Windows\\System32") is None


def test_safe_join_rejects_nul_byte():
    assert safe_join("/srv/uploads", "file\x00.txt") is None


def test_safe_join_empty_directory_returns_none():
    assert safe_join("", "file") is None


def test_safe_join_no_paths_returns_base():
    base = os.path.abspath("/srv/uploads")
    assert safe_join("/srv/uploads") == base


def test_safe_join_does_not_accept_sibling_directory():
    # base="/srv/a" must reject paths that resolve to "/srv/abc" — same
    # prefix string, different directory.
    base = "/srv/a"
    # `safe_join` itself with relative input can't escape (no `..` would help),
    # but the absolute-check guards against absolute inputs that happen to share
    # the prefix string.
    assert safe_join(base, "/srv/abc/file") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path semantics")
def test_safe_join_accepts_mixed_case_drive_on_windows():
    # On Windows, drive-letter casing in user-supplied base must not cause
    # a same-directory descendant to be rejected. Normalisation through
    # `os.path.normcase` makes the comparison case-insensitive.
    result = safe_join("C:\\Users", "Alice/file.txt")
    assert result is not None
    assert result.lower().endswith("alice\\file.txt")


def test_safe_join_descendant_check_uses_normcase(monkeypatch):
    # Force a known case-folding normcase on every platform so the test does
    # not depend on whether the OS's native normcase is identity (POSIX) or
    # case-lowering (Windows).
    monkeypatch.setattr(os.path, "normcase", str.lower)

    base = os.path.abspath("/srv/uploads")
    result = safe_join("/srv/uploads", "FILE.TXT")
    assert result == os.path.join(base, "FILE.TXT")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX root semantics")
def test_safe_join_accepts_descendant_of_posix_root():
    # When base is the filesystem root "/", the descendant check must not
    # compose a prefix of "//" — that would never match a legitimate child.
    result = safe_join("/", "etc")
    assert result == os.path.abspath("/etc")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows root semantics")
def test_safe_join_accepts_descendant_of_windows_drive_root():
    # When base is a Windows drive root "C:\\", the descendant check must
    # not compose a prefix of "C:\\\\" — that would never match a child.
    result = safe_join("C:\\", "Users")
    assert result is not None
    assert result.lower() == os.path.abspath("C:\\Users").lower()


def test_safe_join_equality_branch_with_no_components(monkeypatch):
    # When no path components are supplied, joined == base — exercise the
    # equality branch of the descendant check under a forced normcase.
    monkeypatch.setattr(os.path, "normcase", str.lower)

    base = os.path.abspath("/srv/uploads")
    assert safe_join("/srv/uploads") == base


# ── safe_join Windows device-name rejection ──────────────────────────


@pytest.mark.parametrize(
    "segment",
    ["COM1", "com1", "LPT9", "NUL", "CONIN$", "sub/COM1", "COM1.txt", "COM1.", "COM1 "],
)
def test_safe_join_rejects_windows_device_names(monkeypatch, segment):
    # Force the NT-only guard on so the rejection is exercised on any OS.
    monkeypatch.setattr("veloce.safe._IS_NT", True)
    assert safe_join("/srv/static", segment) is None


@pytest.mark.parametrize("segment", ["com10", "report.txt", "config.ini", "CONfig"])
def test_safe_join_allows_non_device_lookalikes(monkeypatch, segment):
    # Names that merely start with a device token are ordinary files.
    monkeypatch.setattr("veloce.safe._IS_NT", True)
    assert safe_join("/srv/static", segment) is not None


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows abspath maps device names to \\\\.\\COM1 regardless of the guard",
)
def test_safe_join_device_guard_is_noop_off_windows(monkeypatch):
    # On POSIX (_IS_NT False) device names are passed through untouched.
    monkeypatch.setattr("veloce.safe._IS_NT", False)
    assert safe_join("/srv/static", "COM1") is not None
