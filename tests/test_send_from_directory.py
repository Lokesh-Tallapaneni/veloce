"""send_from_directory helper — serve a file with traversal protection."""

from __future__ import annotations

import pytest

from veloce.exceptions import Forbidden
from veloce.helpers import send_from_directory


class TestSendFromDirectory:
    """Test send_from_directory helper."""

    def test_send_existing_file(self, tmp_path):
        test_file = tmp_path / "hello.txt"
        test_file.write_text("Hello World")

        resp = send_from_directory(str(tmp_path), "hello.txt")
        assert resp.body == b"Hello World"

    def test_directory_traversal_of_an_existing_file_is_forbidden(self, tmp_path):
        """The security property, against a file that really is there.

        This previously targeted `../../../etc/passwd` and accepted
        `FileNotFoundError` as success, so it could not tell "traversal blocked"
        from "the file happens not to exist" - and on a machine without
        `/etc/passwd` it passed for the second reason. A real file one level up
        makes the distinction observable.
        """

        outside = tmp_path.parent / "outside-secret.txt"
        outside.write_text("SECRET")
        try:
            with pytest.raises(Forbidden):
                send_from_directory(str(tmp_path), "../outside-secret.txt")
        finally:
            outside.unlink()

    def test_directory_traversal_is_refused_before_the_file_is_looked_up(self, tmp_path):
        """`Forbidden`, not `FileNotFoundError` - the guard runs first."""

        with pytest.raises(Forbidden):
            send_from_directory(str(tmp_path), "../../../etc/passwd")

    def test_an_absent_file_inside_the_root_is_not_forbidden(self, tmp_path):
        """The negative that gives the two above their meaning."""

        with pytest.raises(FileNotFoundError):
            send_from_directory(str(tmp_path), "absent.txt")

    def test_a_path_that_normalises_back_inside_the_root_is_served(self, tmp_path):
        """Refusing every `..` outright would break a legitimate path."""

        (tmp_path / "hello.txt").write_bytes(b"Hello World")
        assert send_from_directory(str(tmp_path), "sub/../hello.txt").body == b"Hello World"
