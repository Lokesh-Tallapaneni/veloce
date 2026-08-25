"""Three values that were accepted and then did nothing.

Same shape as `@app.middleware("http", **kwargs)` and `Blueprint.errorhandler`
already fixed: a caller supplies something, the call succeeds, and the value has
no effect anywhere.

**1. `Namespace.signal(name, doc=...)`** discarded `doc`. The docstring admitted
it ("accepted for API familiarity; it is ignored"), but it is a Blinker-compat
parameter, so code being ported passes it and gets nothing. `Signal` was slotted
without a place to put it — a self-imposed limit, and signals are created once at
import, not per request.

**2. `Config.from_mapping(debug=True)`** stored nothing and returned `True`, so
the caller could not tell. Only UPPERCASE keys are config keys, and the filter is
right — but a **keyword argument** was typed out one key at a time, so dropping it
silently means `DEBUG` is quietly left at its default. A `mapping` is different:
a settings dict or a parsed config section legitimately carries entries that are
not config, so those are still filtered quietly.

**3. `HTTPBearer(scheme_name="Token")`** changed what the server accepts and not
what the OpenAPI document told clients to send:

    __call__ matches      Authorization: Token <credential>
    openapi_scheme() said {"type": "http", "scheme": "bearer"}

The method's own docstring said "published with the scheme it advertises".
"""

from __future__ import annotations

import pytest

from veloce import Namespace, Signal, Veloce
from veloce.config import Config
from veloce.security.http import HTTPBearer
from veloce.testclient import TestClient


def _config() -> Config:
    config = Config()
    config.update(Config.default_config())
    return config


# ── 1. a signal keeps its documentation ──────────────────────────────


def test_a_signal_records_its_doc():
    """The defect: `doc` was discarded."""
    assert Namespace().signal("probe", doc="what it is for").doc == "what it is for"


def test_a_signal_without_a_doc_has_none():
    assert Namespace().signal("probe").doc is None


def test_a_bare_signal_has_none():
    assert Signal("probe").doc is None


def test_a_signal_constructed_directly_can_carry_one():
    assert Signal("probe", "why").doc == "why"


def test_the_same_name_returns_the_same_instance():
    """Memoisation is the existing contract and must not change."""
    namespace = Namespace()
    first = namespace.signal("probe", doc="first")
    assert namespace.signal("probe") is first


def test_the_first_doc_wins():
    """A later call must not rewrite a signal other code already holds."""
    namespace = Namespace()
    namespace.signal("probe", doc="first")
    assert namespace.signal("probe", doc="second").doc == "first"


def test_a_signal_still_sends():
    """The negative: adding a slot must not disturb delivery."""
    namespace = Namespace()
    signal = namespace.signal("probe", doc="d")
    seen = []

    def receiver(sender, **kw):
        seen.append(kw.get("value"))

    # Held in a local: `connect` keeps a weak reference by default, so a lambda
    # with no other referent is collected before `send` runs.
    signal.connect(receiver)
    signal.send(None, value=7)
    assert seen == [7]


def test_two_names_are_two_signals():
    namespace = Namespace()
    assert namespace.signal("a", doc="x") is not namespace.signal("b", doc="y")


# ── 2. a lowercase config keyword is refused ─────────────────────────


@pytest.mark.parametrize("key", ["debug", "Debug", "my_setting", "testing"])
def test_a_non_uppercase_keyword_is_refused(key):
    """The defect: stored nothing, returned True, said nothing."""
    with pytest.raises(TypeError, match="must be UPPERCASE"):
        _config().from_mapping(**{key: True})


def test_the_message_shows_the_uppercase_form():
    with pytest.raises(TypeError, match="DEBUG"):
        _config().from_mapping(debug=True)


def test_several_bad_keywords_are_all_named():
    with pytest.raises(TypeError, match="alpha, beta"):
        _config().from_mapping(beta=1, alpha=2)


def test_an_uppercase_keyword_is_stored():
    """The negative: refusing everything would pass the tests above vacuously."""
    config = _config()
    assert config.from_mapping(TESTING=True) is True
    assert config["TESTING"] is True


def test_a_mapping_still_filters_quietly():
    """A settings dict may legitimately carry entries that are not config."""
    config = _config()
    config.from_mapping({"DEBUG": True, "debug": False, "note": "x"})
    assert config["DEBUG"] is True
    assert "note" not in config
    assert "debug" not in config


def test_a_mapping_and_keywords_combine():
    config = _config()
    config.from_mapping({"DEBUG": True}, TESTING=True)
    assert config["DEBUG"] is True
    assert config["TESTING"] is True


def test_a_bad_keyword_is_refused_before_anything_is_stored():
    """A partial application would be worse than either outcome."""
    config = _config()
    with pytest.raises(TypeError):
        config.from_mapping({"DEBUG": True}, testing=True)
    assert config["DEBUG"] is False


def test_no_arguments_is_still_fine():
    assert _config().from_mapping() is True


# ── 3. the published bearer scheme is the one enforced ───────────────


def test_the_default_scheme_is_bearer():
    assert HTTPBearer().openapi_scheme() == {"type": "http", "scheme": "bearer"}


def test_a_custom_scheme_reaches_the_document():
    """The defect: this published `bearer` while the server matched `Token`."""
    assert HTTPBearer(scheme_name="Token").openapi_scheme() == {
        "type": "http",
        "scheme": "token",
    }


def test_the_published_scheme_is_lower_cased():
    """OpenAPI 3.1 names the IANA registry entry, whose entries are lower-case."""
    assert HTTPBearer(scheme_name="BEARER").openapi_scheme()["scheme"] == "bearer"


@pytest.mark.parametrize("scheme", ["Bearer", "Token", "DPoP"])
def test_the_document_and_the_runtime_agree(scheme):
    """The property: what is published is what is accepted."""
    from veloce import Depends

    app = Veloce(openapi_url=None)
    guard = HTTPBearer(scheme_name=scheme)

    @app.get("/private")
    async def private(credential: str = Depends(guard)) -> dict:
        return {"token": credential}

    client = TestClient(app)
    published = guard.openapi_scheme()["scheme"]
    # A client following the document sends the scheme it names.
    accepted = client.get("/private", headers={"Authorization": f"{published} abc123"})
    assert accepted.status_code == 200
    assert accepted.json() == {"token": "abc123"}


def test_a_different_scheme_is_still_refused():
    """The negative: accepting anything would pass the test above vacuously."""
    from veloce import Depends

    app = Veloce(openapi_url=None)

    @app.get("/private")
    async def private(credential=Depends(HTTPBearer(scheme_name="Token"))) -> dict:
        return {}

    assert TestClient(app).get("/private", headers={"Authorization": "Bearer x"}).status_code == 401


def test_the_scheme_reaches_the_openapi_document():
    """End to end: through the generated document, not just the method."""
    from veloce import Depends

    app = Veloce(title="S", version="1.0.0")

    @app.get("/private")
    async def private(credential=Depends(HTTPBearer(scheme_name="Token"))) -> dict:
        return {}

    schemes = app.openapi()["components"]["securitySchemes"]
    assert any(entry.get("scheme") == "token" for entry in schemes.values())
