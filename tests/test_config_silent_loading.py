"""`silent=` means the same thing in every config loader, and so does encoding.

The three file loaders each carried their own `try/except OSError: if silent`
block. Extracting one reader is only safe if it preserves what each loader
actually opened with: `from_env_file` pinned utf-8 and `from_file` left a
text-mode load on the platform default, and folding those together would
silently change what a `.env` holding non-ASCII reads as on a machine whose
default is not utf-8 - which is every Windows machine this is developed on.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from veloce.config import Config

LOADERS = ("from_pyfile", "from_env_file")


@pytest.mark.parametrize("loader", LOADERS)
def test_a_missing_file_is_silent_when_asked(loader: str, tmp_path: pathlib.Path) -> None:
    config = Config()
    assert getattr(config, loader)(str(tmp_path / "absent"), silent=True) is False


@pytest.mark.parametrize("loader", LOADERS)
def test_a_missing_file_raises_by_default(loader: str, tmp_path: pathlib.Path) -> None:
    config = Config()
    with pytest.raises(OSError):
        getattr(config, loader)(str(tmp_path / "absent"))


def test_from_file_is_silent_and_loud_the_same_way(tmp_path: pathlib.Path) -> None:
    config = Config()
    assert config.from_file(str(tmp_path / "absent.json"), silent=True) is False
    with pytest.raises(OSError):
        config.from_file(str(tmp_path / "absent.json"))


def test_an_env_file_is_read_as_utf8(tmp_path: pathlib.Path) -> None:
    """Not the platform default, which on Windows is cp1252."""
    env = tmp_path / ".env"
    env.write_bytes("NAME=caf\u00e9\n".encode())
    config = Config()
    assert config.from_env_file(str(env)) is True
    assert config["NAME"] == "caf\u00e9"


def test_a_present_file_still_loads_through_each_loader(tmp_path: pathlib.Path) -> None:
    py = tmp_path / "settings.py"
    py.write_text('SECRET_KEY = "s"\nlowercase = 1\n', encoding="utf-8")
    config = Config()
    assert config.from_pyfile(str(py)) is True
    assert config["SECRET_KEY"] == "s"
    assert "lowercase" not in config

    js = tmp_path / "settings.json"
    js.write_text(json.dumps({"TIMEOUT": 30}), encoding="utf-8")
    assert config.from_file(str(js)) is True
    assert config["TIMEOUT"] == 30


def test_an_error_from_the_loader_itself_is_not_swallowed(tmp_path: pathlib.Path) -> None:
    """`silent=` covers a missing file, not a malformed one."""
    js = tmp_path / "bad.json"
    js.write_bytes(b"{not json")
    # Deliberately broad: the loader is the caller's callable, so *what* it
    # raises on malformed input is not this module's contract. What is, and
    # is asserted below, is that it is not an `OSError` - the class `silent=`
    # swallows.
    with pytest.raises(Exception) as excinfo:  # noqa: B017 - see above
        Config().from_file(str(js), silent=True)
    assert not isinstance(excinfo.value, OSError)
