"""`app.settings` is a typed, read-only view over `app.config`.

The `Settings` dataclass is the single declaration of the keys Veloce reads, so
these tests pin the three things that make it trustworthy: it agrees with the
config the app is seeded with, it follows later changes to `app.config`, and it
cannot be written through.
"""

from __future__ import annotations

import dataclasses

import pytest

from veloce import Settings, Veloce
from veloce.config import Config


def test_defaults_match_the_seeded_config():
    app = Veloce(openapi_url=None)
    assert Settings.defaults() == Config.default_config()
    assert {k: v for k, v in app.config.items() if k in Settings.defaults()} == {
        **Settings.defaults(),
        "DEBUG": False,
    }


def test_every_field_has_a_description():
    missing = [f.name for f in dataclasses.fields(Settings) if not f.metadata.get("description")]
    assert missing == []


def test_every_field_is_an_uppercase_config_key():
    assert all(Config._is_uppercase_key(f.name) for f in dataclasses.fields(Settings))


def test_settings_follows_later_config_changes():
    app = Veloce(openapi_url=None)
    assert app.settings.REQUEST_TIMEOUT == 30
    app.config["REQUEST_TIMEOUT"] = 5
    assert app.settings.REQUEST_TIMEOUT == 5


def test_constructor_debug_is_visible():
    assert Veloce(openapi_url=None, debug=True).settings.DEBUG is True


def test_unknown_config_keys_stay_allowed_and_are_ignored_by_settings():
    app = Veloce(openapi_url=None)
    app.config["MY_EXTENSION_KEY"] = 1
    assert app.config["MY_EXTENSION_KEY"] == 1
    assert not hasattr(app.settings, "MY_EXTENSION_KEY")


def test_a_key_removed_from_config_falls_back_to_the_default():
    app = Veloce(openapi_url=None)
    del app.config["KEEP_ALIVE_TIMEOUT"]
    assert app.settings.KEEP_ALIVE_TIMEOUT == 75


def test_assignment_is_refused_and_points_at_config():
    settings = Veloce(openapi_url=None).settings
    with pytest.raises(AttributeError, match=r"app\.config\['DEBUG'\]"):
        settings.DEBUG = True  # type: ignore[misc]


def test_deletion_is_refused():
    settings = Veloce(openapi_url=None).settings
    with pytest.raises(AttributeError, match="read-only"):
        del settings.DEBUG


def test_settings_is_hashable_and_slotted():
    settings = Veloce(openapi_url=None).settings
    assert not hasattr(settings, "__dict__")
    assert hash(settings) == hash(Veloce(openapi_url=None).settings)


def test_a_snapshot_is_not_changed_by_later_config_edits():
    app = Veloce(openapi_url=None)
    before = app.settings
    app.config["REQUEST_TIMEOUT"] = 1
    assert before.REQUEST_TIMEOUT == 30
