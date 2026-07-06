"""send_from_directory helper — serve a file with traversal protection."""

from __future__ import annotations

import pytest


class TestSendFromDirectory:
    """Test send_from_directory helper."""

    def test_send_existing_file(self, tmp_path):
        test_file = tmp_path / "hello.txt"
        test_file.write_text("Hello World")

        from veloce.helpers import send_from_directory

        resp = send_from_directory(str(tmp_path), "hello.txt")
        assert resp.body == b"Hello World"

    def test_directory_traversal_blocked(self, tmp_path):
        from veloce.exceptions import HTTPException
        from veloce.helpers import send_from_directory

        with pytest.raises((HTTPException, FileNotFoundError)):
            send_from_directory(str(tmp_path), "../../../etc/passwd")
