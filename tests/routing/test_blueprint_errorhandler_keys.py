"""A blueprint refuses an error-handler key that can never match.

Registering a handler under a key the dispatcher cannot look up succeeded
silently, so the handler never fired and nothing said why.

Split out of `test_entry_point_parity.py` - see that module's history in
`test_config_env_loaders.py`.
"""

from __future__ import annotations

import pytest

from veloce import Blueprint, Veloce
from veloce.testclient import TestClient


@pytest.mark.parametrize("key", ["500", "ValueError", 404.0, object, None, ("a",)])
def test_a_blueprint_refuses_an_unmatchable_key(key):
    """The defect: these were accepted and then never fired."""
    bp = Blueprint("shop")
    with pytest.raises(TypeError, match="error handler keys"):
        bp.errorhandler(key)(lambda exc: None)


@pytest.mark.parametrize("key", [500, 404, ValueError, KeyError, Exception])
def test_a_blueprint_accepts_a_valid_key(key):
    bp = Blueprint("shop")
    bp.errorhandler(key)(lambda exc: None)


def test_the_string_message_says_what_to_write_instead():
    bp = Blueprint("shop")
    with pytest.raises(TypeError, match="Write 500 without the quotes"):
        bp.errorhandler("500")(lambda exc: None)

    with pytest.raises(TypeError, match="Pass the class itself"):
        bp.errorhandler("ValueError")(lambda exc: None)


def test_the_two_registration_levels_agree():
    """The property: what the app refuses, a blueprint must refuse."""
    app = Veloce(openapi_url=None)
    bp = Blueprint("shop")
    for key in ("500", "ValueError", 404.0):
        with pytest.raises(TypeError):
            app.errorhandler(key)(lambda exc: None)
        with pytest.raises(TypeError):
            bp.errorhandler(key)(lambda exc: None)


def test_a_valid_blueprint_handler_still_fires():
    """The negative: the check must not break registration that worked."""
    app = Veloce(openapi_url=None)
    bp = Blueprint("shop", url_prefix="/shop")

    @bp.errorhandler(ValueError)
    async def on_value(exc):
        return {"handled": "by-class-key"}

    @bp.get("/boom")
    async def boom() -> dict:
        raise ValueError("nope")

    app.register_blueprint(bp)
    assert TestClient(app).get("/shop/boom").json() == {"handled": "by-class-key"}
