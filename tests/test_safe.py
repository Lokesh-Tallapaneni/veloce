"""secure_filename + safe_join + constant_time_compare tests (SEC11, SEC12, S8)."""

from __future__ import annotations

import os

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
