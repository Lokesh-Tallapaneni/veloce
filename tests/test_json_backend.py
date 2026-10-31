"""The JSON seam behaves the same whichever backend is installed.

`veloce._json` owns the one import of a JSON library and gives the rest of the
package a fixed surface. Fifty-odd call sites used to name a third-party API
directly, which made the library a hard requirement: `import orjson` at module
scope fails before any `JSONProvider` is consulted, and no PEP 508 marker can
say "except on a free-threaded build".

Every assertion here is parametrised over each backend that is installed, and
both must agree. That is the whole point: a payload must not serialise one way
in a deployment that got the fast library and another way in one that did not.
The behaviour pinned below was captured from the library this project already
depends on, so the seam preserves what shipped rather than inventing a
contract.
"""

from __future__ import annotations

import datetime
import importlib.util
import uuid

import orjson as _orjson
import pytest
from pydantic import BaseModel

import veloce
from veloce import (
    DefaultJSONProvider,
    JSONProvider,
    ORJSONResponse,
    Signer,
    TestClient,
    Veloce,
    _json,
)
from veloce.cache import _default_key

_HAS_UJSON = importlib.util.find_spec("ujson") is not None

try:
    import msgspec as _msgspec

    _HAS_MSGSPEC = True
except ImportError:  # pragma: no cover - the no-msgspec CI leg
    _HAS_MSGSPEC = False

#: Each backend that can be built here. One entry means only one is installed;
#: the suite still pins the contract for it.
BACKENDS = _json.available_backends()


@pytest.fixture(params=BACKENDS, ids=list(BACKENDS))
def backend(request: pytest.FixtureRequest):
    """A built backend: the same trio `_json` exposes at module scope."""
    return _json.build_backend(request.param)


# ── the surface the package relies on ────────────────────────────────


def test_the_module_reports_which_backend_is_live():
    """An operator must be able to answer this without guessing."""
    assert _json.backend in BACKENDS
    assert isinstance(_json.backend, str)


def test_the_module_level_names_are_bound():
    assert callable(_json.dumps)
    assert callable(_json.loads)
    assert issubclass(_json.JSONDecodeError, ValueError)


# ── wire format: identical across backends ───────────────────────────


def test_dumps_returns_bytes(backend):
    assert isinstance(backend.dumps({"a": 1}), bytes)


def test_dumps_is_compact(backend):
    """A space after the colon is a wire difference; it changes ETags."""
    assert backend.dumps({"a": 1, "b": 2}) == b'{"a":1,"b":2}'


def test_loads_accepts_bytes_and_str(backend):
    assert backend.loads(b'{"a":1}') == {"a": 1}
    assert backend.loads('{"a":1}') == {"a": 1}


def test_a_round_trip_preserves_the_value(backend):
    value = {"s": "x", "i": 1, "f": 1.5, "b": True, "n": None, "l": [1, 2]}
    assert backend.loads(backend.dumps(value)) == value


# ── sorted keys: cache keys depend on this ───────────────────────────


def test_sort_keys_orders_the_output(backend):
    """`cache.py` builds cache keys with this option.

    An unsorted fallback would not raise - it would silently produce
    different keys, so a warm cache would start missing.
    """
    assert backend.dumps({"b": 1, "a": 2}, option=_json.OPT_SORT_KEYS) == b'{"a":2,"b":1}'


def test_sort_keys_is_stable_for_equal_mappings_built_differently(backend):
    """Two dicts with the same items must key identically whatever the order."""
    first = backend.dumps({"z": 1, "m": 2, "a": 3}, option=_json.OPT_SORT_KEYS)
    second = backend.dumps({"a": 3, "z": 1, "m": 2}, option=_json.OPT_SORT_KEYS)
    assert first == second


def test_sort_keys_reaches_nested_mappings(backend):
    nested = {"b": {"d": 1, "c": 2}, "a": 3}
    assert backend.dumps(nested, option=_json.OPT_SORT_KEYS) == b'{"a":3,"b":{"c":2,"d":1}}'


def test_indent_two_formats_the_output(backend):
    assert backend.dumps({"a": 1}, option=_json.OPT_INDENT_2) == b'{\n  "a": 1\n}'


# ── the default= hook ────────────────────────────────────────────────


class _Unserialisable:
    """A value no backend knows, so every one must route it to `default`."""


def test_an_unserialisable_value_goes_through_default(backend):
    assert backend.dumps(_Unserialisable(), default=lambda _: "x") == b'"x"'


def test_default_is_not_consulted_for_a_value_the_backend_knows(backend):
    calls: list[object] = []

    def spy(value: object) -> object:
        calls.append(value)
        return None

    assert backend.dumps({"a": 1}, default=spy) == b'{"a":1}'
    assert calls == []


# ── error types the package already catches ──────────────────────────


def test_bad_input_raises_a_value_error(backend):
    """Call sites catch `JSONDecodeError`, which must stay a ValueError."""
    with pytest.raises(ValueError):
        backend.loads(b"{")
    with pytest.raises(backend.JSONDecodeError):
        backend.loads(b"{")


def test_an_unserialisable_value_raises_a_type_error(backend):
    """orjson's JSONEncodeError *is* TypeError, so handlers catch TypeError.

    msgspec raises `EncodeError`, which is not, so the seam maps it.
    """
    with pytest.raises(TypeError):
        backend.dumps(_Unserialisable())
    with pytest.raises(TypeError):
        backend.dumps(1 + 2j)


def test_a_default_that_raises_propagates_as_a_type_error(backend):
    def boom(value: object) -> object:
        raise TypeError("nope")

    with pytest.raises(TypeError):
        backend.dumps(_Unserialisable(), default=boom)


def test_only_msgspec_encodes_a_set(backend):
    """The backends disagree about a `set`, so the difference is pinned.

    Matching it either way would mean walking the object graph on every
    encode, which this path cannot afford - so this fails loudly if a library
    changes its mind rather than letting the divergence drift.
    """
    if backend.name == "msgspec":
        assert backend.dumps({1, 2}) == b"[1,2]"
    else:
        with pytest.raises(TypeError):
            backend.dumps({1, 2})


def test_only_orjson_refuses_an_integer_wider_than_64_bits(backend):
    if backend.name == "orjson":
        with pytest.raises(TypeError):
            backend.dumps(2**70)
    else:
        assert backend.dumps(2**70) == b"1180591620717411303424"


def test_only_orjson_refuses_a_non_string_mapping_key(backend):
    if backend.name == "orjson":
        with pytest.raises(TypeError):
            backend.dumps({1: "a"})
    else:
        assert backend.dumps({1: "a"}) == b'{"1":"a"}'


# ── values with backend-specific handling, pinned ────────────────────


def test_not_a_number_is_emitted_as_null(backend):
    """Captured from the shipped library: NaN and Infinity become null."""
    assert backend.dumps(float("nan")) == b"null"
    assert backend.dumps(float("inf")) == b"null"


def test_a_naive_datetime_serialises_as_iso_8601(backend):
    moment = datetime.datetime(2026, 1, 2, 3, 4, 5)
    assert backend.dumps(moment) == b'"2026-01-02T03:04:05"'


def test_an_empty_container_round_trips(backend):
    assert backend.dumps({}) == b"{}"
    assert backend.dumps([]) == b"[]"


def test_non_ascii_is_emitted_as_utf8_not_escaped(backend):
    assert backend.dumps({"k": "café"}) == '{"k":"café"}'.encode()


# ── the selector: one place, applied at import ───────────────────────


def test_an_unknown_backend_name_is_refused(monkeypatch):
    """A typo must not silently serve the default."""
    monkeypatch.setenv(_json._ENV_VAR, "nosuchlib")
    with pytest.raises(ImportError, match="not a known JSON backend"):
        _json._select()


def test_build_backend_rejects_an_unknown_name():
    with pytest.raises(ValueError, match="unknown JSON backend"):
        _json.build_backend("nosuchlib")


@pytest.mark.parametrize("name", BACKENDS)
def test_the_env_var_pins_the_named_backend(name, monkeypatch):
    """The whole process takes its backend from this one place."""
    monkeypatch.setenv(_json._ENV_VAR, name)
    assert _json._select().name == name


def test_a_named_backend_that_is_missing_raises_rather_than_falling_back(monkeypatch):
    """Falling through would serialise differently than asked, without saying so."""
    monkeypatch.setenv(_json._ENV_VAR, "orjson")
    monkeypatch.setitem(
        _json._BUILDERS, "orjson", lambda: (_ for _ in ()).throw(ImportError("no wheel"))
    )
    with pytest.raises(ImportError, match="was requested but could not be imported"):
        _json._select()


def test_an_empty_env_var_falls_back_to_preference_order(monkeypatch):
    monkeypatch.setenv(_json._ENV_VAR, "   ")
    assert _json._select().name == BACKENDS[0]


def test_an_unset_env_var_selects_the_first_available(monkeypatch):
    monkeypatch.delenv(_json._ENV_VAR, raising=False)
    assert _json._select().name == BACKENDS[0]


# ── use_json_backend: the in-code selector ───────────────────────────


@pytest.fixture
def restore_backend():
    """Put the process back on the backend it started with."""
    original = _json.backend
    yield
    _json.use_json_backend(original)


@pytest.mark.parametrize("name", BACKENDS)
def test_use_json_backend_switches_the_live_backend(name, restore_backend):
    _json.use_json_backend(name)
    assert _json.backend == name


@pytest.mark.parametrize("name", BACKENDS)
def test_the_enum_and_the_string_both_work(name, restore_backend):
    _json.use_json_backend(_json.JSONBackend(name))
    assert _json.backend == name
    _json.use_json_backend(name)
    assert _json.backend == name


def test_the_switch_reaches_every_module_that_encodes(restore_backend):
    """One call has to move all of them, or the choice is not a choice."""

    def handler() -> None: ...

    before = (
        _json.dumps({"b": 2, "a": 1}),
        Signer(secret="s").dumps({"u": 1}).split(".")[0],
        _default_key(handler, (), {"a": 1, "b": 2}),
    )
    for name in BACKENDS:
        _json.use_json_backend(name)
        after = (
            _json.dumps({"b": 2, "a": 1}),
            Signer(secret="s").dumps({"u": 1}).split(".")[0],
            _default_key(handler, (), {"a": 1, "b": 2}),
        )
        assert after == before, f"{name} diverged from {_json.backend}"


def test_a_switch_changes_what_a_response_is_encoded_with(restore_backend):
    app = Veloce()

    @app.get("/")
    async def index(request):  # noqa: ANN001, ANN202
        return {"b": 2, "a": 1}

    for name in BACKENDS:
        _json.use_json_backend(name)
        with TestClient(app) as client:
            assert client.get("/").body == b'{"b":2,"a":1}'


def test_an_unknown_name_is_refused_with_the_choices_listed(restore_backend):
    with pytest.raises(ValueError, match="unknown JSON backend"):
        _json.use_json_backend("nosuchlib")
    assert _json.backend in BACKENDS


def test_selecting_the_live_backend_again_is_a_no_op(restore_backend):
    live = _json.backend
    dumps_before = _json.dumps
    _json.use_json_backend(live)
    assert _json.dumps is dumps_before


def test_the_names_are_exported_from_the_package():
    assert veloce.use_json_backend is _json.use_json_backend
    assert veloce.JSONBackend is _json.JSONBackend
    assert "use_json_backend" in veloce.__all__
    assert "JSONBackend" in veloce.__all__


# ── the JSON backend is orthogonal to the model library ──────────────
# The models live at module scope because `from __future__ import annotations`
# turns a handler's annotation into a string the resolver looks up in module
# globals - a class defined inside a test function is not reachable there.


class _PydanticItem(BaseModel):
    name: str
    qty: int


if _HAS_MSGSPEC:

    class _StructItem(_msgspec.Struct):
        name: str
        qty: int


@pytest.mark.parametrize("name", BACKENDS)
def test_a_pydantic_model_round_trips_on_every_backend(name, restore_backend):
    """Validation is `ModelBackend`'s axis; encoding is this one's."""
    _json.use_json_backend(name)
    app = Veloce()

    @app.post("/items")
    async def create(item: _PydanticItem) -> _PydanticItem:
        return item

    with TestClient(app) as client:
        body = client.post("/items", json={"name": "bolt", "qty": 3}).body
        assert body == b'{"name":"bolt","qty":3}'
        assert client.post("/items", json={"name": "bolt", "qty": "x"}).status_code == 422


@pytest.mark.skipif(not _HAS_MSGSPEC, reason="msgspec is not installed")
@pytest.mark.parametrize("name", BACKENDS)
def test_a_msgspec_struct_round_trips_on_every_backend(name, restore_backend):
    """A `msgspec.Struct` model validates the same whichever library encodes.

    Choosing orjson for JSON does not force Pydantic for models: the struct is
    converted to builtins, then encoded by whichever backend is live.
    """
    _json.use_json_backend(name)
    app = Veloce()

    @app.post("/structs")
    async def create(item: _StructItem) -> _StructItem:
        return item

    with TestClient(app) as client:
        body = client.post("/structs", json={"name": "bolt", "qty": 3}).body
        assert body == b'{"name":"bolt","qty":3}'
        assert client.post("/structs", json={"name": "bolt", "qty": "x"}).status_code == 422


# ── what the text backends had to close, not document ────────────────
# `json` and `ujson` emit a bare `NaN` / `Infinity`, which no JSON parser
# accepts, and neither encodes a date or a `UUID` unaided. Both would make the
# same payload behave differently depending on which library was installed.


def test_a_non_finite_float_is_null_on_every_backend(backend):
    assert backend.dumps(float("nan")) == b"null"
    assert backend.dumps(float("inf")) == b"null"
    assert backend.dumps(float("-inf")) == b"null"


def test_a_non_finite_float_nested_in_a_container_is_null(backend):
    assert backend.dumps({"x": [1.0, float("nan")]}) == b'{"x":[1.0,null]}'


def test_a_date_and_a_uuid_encode_without_a_default_hook(backend):
    assert backend.dumps(datetime.date(2026, 1, 2)) == b'"2026-01-02"'
    assert backend.dumps(datetime.time(3, 4, 5)) == b'"03:04:05"'
    assert backend.dumps(uuid.UUID(int=1)) == b'"00000000-0000-0000-0000-000000000001"'


def test_the_native_types_win_over_a_default_hook(backend):
    """orjson handles a date itself and never consults `default=` for one."""
    assert backend.dumps(datetime.date(2026, 1, 2), default=lambda _: "HOOK") == b'"2026-01-02"'


# ── precedence: what wins when the choices disagree ──────────────────


class _OrjsonProvider(JSONProvider):
    """A provider that names a library itself, rather than taking the backend."""

    def dumps(self, obj, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return _orjson.dumps(obj)

    def loads(self, data):  # noqa: ANN001, ANN201
        return _orjson.loads(data)


@pytest.mark.skipif(not _HAS_UJSON, reason="ujson is not installed")
def test_a_response_class_does_not_override_the_backend(restore_backend):
    """`ORJSONResponse` is an alias for `JSONResponse`; it binds no library.

    It therefore encodes with whatever backend is live, so the class name does
    not select one.
    """
    _json.use_json_backend("ujson")
    app = Veloce()

    @app.get("/", response_class=ORJSONResponse)
    async def index(request):  # noqa: ANN001, ANN202
        return {"b": 2, "a": 1}

    with TestClient(app) as client:
        assert client.get("/").body == b'{"b":2,"a":1}'


@pytest.mark.skipif(not _HAS_UJSON, reason="ujson is not installed")
def test_the_stock_provider_follows_the_backend(restore_backend):
    _json.use_json_backend("ujson")
    app = Veloce()
    app.json_provider_class = DefaultJSONProvider

    @app.get("/")
    async def index(request):  # noqa: ANN001, ANN202
        return {"b": 2, "a": 1}

    with TestClient(app) as client:
        assert client.get("/").body == b'{"b":2,"a":1}'
    assert _json.backend == "ujson"


@pytest.mark.skipif(not _HAS_UJSON, reason="ujson is not installed")
def test_a_provider_that_names_a_library_wins_for_bodies_only(restore_backend):
    """The one documented way to split the two, and it stops at the body.

    Cache keys and signed tokens stay on the backend, so they remain
    canonical whatever an application does to its own output.
    """
    _json.use_json_backend("ujson")
    app = Veloce()
    app.json_provider_class = _OrjsonProvider

    @app.get("/")
    async def index(request):  # noqa: ANN001, ANN202
        return {"b": 2, "a": 1}

    with TestClient(app) as client:
        assert client.get("/").body == b'{"b":2,"a":1}'

    # The provider does not reach either wire format.
    assert _json.backend == "ujson"
    assert Signer(secret="s").dumps({"u": 1})
    assert _default_key(_handler_for_keys, (), {"a": 1})


def _handler_for_keys() -> None: ...
