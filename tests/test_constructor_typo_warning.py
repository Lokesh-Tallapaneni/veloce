"""A misspelled constructor parameter is reported instead of quietly absorbed.

`Veloce(**extra)` is an open namespace on purpose — an extension or an OpenAPI
customisation may put anything in `app.extra`. That openness meant a typo was
indistinguishable from a deliberate key:

    Veloce(tittle="My API", debugg=True)
      app.title -> "Veloce"                   the value is ignored
      app.extra -> {"tittle": ..., "debugg": ...}

No error, no warning, no log line. The app served the default title and behaved
as though the arguments had never been passed. Same shape as the two silent-drop
defects fixed as 2.28 and 2.31 in `AUDIT-2026-08-25.md`.

Refusing every unknown key would break the feature, so the test is *nearness*: a
key one or two edits from a real parameter name is almost certainly a typo and is
warned about; an unrelated key is left alone. The check runs only when `extra` is
non-empty, so an app that passes none pays nothing.
"""

from __future__ import annotations

import subprocess
import sys
import warnings

import pytest

from veloce import Veloce
from veloce.testclient import TestClient


def _warnings_for(**kwargs) -> list[str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Veloce(openapi_url=None, **kwargs)
    return [str(w.message) for w in caught if issubclass(w.category, UserWarning)]


# ── a near miss is reported ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("typo", "meant"),
    [
        ("tittle", "title"),
        ("titel", "title"),
        ("debugg", "debug"),
        ("redoc_uri", "redoc_url"),
        ("docs_uri", "docs_url"),
        ("openapi_uri", "openapi_url"),
        ("versoin", "version"),
        ("prefixx", "prefix"),
        ("lifespanx", "lifespan"),
        ("root_paths", "root_path"),
    ],
)
def test_a_misspelled_parameter_is_reported(typo, meant):
    """The defect: every one of these was absorbed in silence."""
    messages = _warnings_for(**{typo: "value"})
    assert messages, f"{typo} produced no warning"
    assert typo in messages[0]
    assert meant in messages[0]


def test_the_message_says_the_value_is_not_used():
    """The consequence is the part that matters, not the spelling."""
    message = _warnings_for(tittle="My API")[0]
    assert "app.extra" in message
    assert "keeps its default" in message


def test_the_warning_is_a_user_warning():
    """Visible by default; a DeprecationWarning would be hidden."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Veloce(openapi_url=None, tittle="x")
    assert [w.category for w in caught] == [UserWarning]


def test_several_typos_are_each_reported():
    messages = _warnings_for(tittle="a", debugg=True, redoc_uri="/r")
    assert len(messages) == 3


def test_the_value_is_still_reachable_in_extra():
    """Warned about, not dropped - the caller can still find what they passed."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        app = Veloce(openapi_url=None, tittle="My API")
    assert app.extra["tittle"] == "My API"
    assert app.title == "Veloce"


# ── a genuine extension key is left alone ────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "my_extension_key",
        "x",
        "sentry_dsn",
        "tenant",
        "feature_flags",
        "db",
        "cache_backend",
        "_private",
        "OTEL_ENDPOINT",
    ],
)
def test_an_unrelated_key_is_not_reported(key):
    """The namespace is open; only a near miss is suspicious."""
    assert _warnings_for(**{key: "value"}) == []


def test_an_unrelated_key_is_kept():
    app = Veloce(openapi_url=None, sentry_dsn="https://example")
    assert app.extra["sentry_dsn"] == "https://example"


def test_a_mix_reports_only_the_typo():
    messages = _warnings_for(tittle="a", sentry_dsn="b")
    assert len(messages) == 1
    assert "tittle" in messages[0]


def test_no_extra_produces_no_warning():
    assert _warnings_for() == []


def test_a_correctly_spelled_parameter_produces_no_warning():
    """It is a real parameter, so it never reaches `extra` at all."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        app = Veloce(openapi_url=None, title="My API", debug=True)
    assert [w for w in caught if issubclass(w.category, UserWarning)] == []
    assert app.title == "My API"
    assert app.extra == {}


# ── the app is otherwise unaffected ──────────────────────────────────


def test_the_app_still_works_with_a_typo():
    """A warning, not a refusal - the app must still serve."""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        app = Veloce(openapi_url=None, tittle="My API")

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    assert TestClient(app).get("/x").json() == {"ok": True}


def test_the_check_is_skipped_when_there_is_nothing_to_check():
    """It imports `difflib` and `inspect`; neither should load for a bare app."""
    code = (
        "import sys; sys.path.insert(0, 'src');"
        "import veloce; veloce.Veloce(openapi_url=None);"
        "print('difflib' in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.stdout.strip() == "False", result.stderr


def test_the_warning_points_at_the_caller():
    """`stacklevel` must name the user's line, not a line inside veloce."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Veloce(openapi_url=None, tittle="x")
    assert caught[0].filename.endswith("test_constructor_typo_warning.py")


# ── the threshold behaves sensibly ───────────────────────────────────


@pytest.mark.parametrize("key", ["ttl", "url", "t", "id"])
def test_a_short_unrelated_key_is_not_reported(key):
    """Short strings match loosely; the cutoff must not fire on them."""
    assert _warnings_for(**{key: 1}) == []


def test_a_completely_different_word_is_not_reported():
    assert _warnings_for(elephant="grey") == []


def test_an_exact_extension_name_containing_a_parameter_is_not_reported():
    """`title_prefix` is a plausible extension key, not a typo of `title`."""
    assert _warnings_for(title_prefix="ACME ") == []
