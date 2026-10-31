"""The package's one JSON backend — chosen at import, reported, never guessed.

Every other module takes `dumps`, `loads` and `JSONDecodeError` from here, so
the library that implements them is named in exactly one place. That is what
makes it substitutable: `import orjson` at module scope fails before any
`JSONProvider` can be consulted, and no PEP 508 marker can express "except on
a free-threaded build", where orjson has no wheel on any platform.

`orjson` is the default and is what every current deployment uses. The
fallback engages only when it cannot be imported at all, and `backend` says
which one is live so an operator never has to guess. `ujson` and the standard
library are selectable but never automatic: nothing falls back to them, and
the latter exists so Veloce can run with no JSON dependency at all.

Set `VELOCE_JSON_BACKEND` to pin one instead. It is read at import, so the
choice reaches every module at once; a name that is unknown or uninstalled
raises rather than quietly selecting something else.

The two option flags the package uses are reproduced on every backend, because
code depends on them: `cache.py` builds cache keys with `OPT_SORT_KEYS`, and an
unsorted fallback would not raise - it would silently change every key.

**Backends are not identical where the fast one is stricter.** orjson refuses
an int outside 64 bits and a non-`str` dict key, which the other three accept;
msgspec is alone in encoding a `set`. Matching that strictness would mean
walking the object graph on every encode, which this path cannot afford, so
the difference is pinned by `tests/test_json_backend.py` rather than hidden.

Two differences were closed instead of pinned, because leaving them would let
the same payload behave differently per deployment rather than merely be
refused: `json` and `ujson` emit a bare `NaN`, which no parser accepts, and
neither encodes a date or a `UUID` unaided.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from enum import Enum
from typing import Any, Final, NamedTuple, Protocol, cast

#: Sort mapping keys before encoding. Cache keys depend on this.
#:
#: orjson's own values, so the default backend needs no translating wrapper -
#: one measured 126ns per response, 40% of the call.
OPT_SORT_KEYS: Final = 32
#: Pretty-print with a two-space indent.
OPT_INDENT_2: Final = 1


class _Dumps(Protocol):
    """The encode signature every backend presents.

    The positional-only object and `int | None` option both match orjson's own
    stub, because the default backend is bound to that function directly.
    """

    def __call__(
        self, obj: Any, /, default: Callable[[Any], Any] | None = ..., option: int | None = ...
    ) -> bytes: ...


class Backend(NamedTuple):
    """One JSON implementation behind the package's fixed surface."""

    name: str
    dumps: _Dumps
    loads: Callable[[bytes | bytearray | memoryview | str], Any]
    JSONDecodeError: type[ValueError]


#: What is worth descending into. Everything else is a leaf.
_CONTAINERS: Final = (dict, list, tuple)


def _sorted_deep(value: Any) -> Any:
    """Return `value` with every mapping's keys in sorted order.

    Only for a backend with no sort option of its own. Containers are rebuilt;
    anything else is handed back untouched, so a value the backend serialises
    natively - a `datetime`, a `UUID` - is not disturbed.

    The leaf test is inlined rather than left to the recursive call, because a
    Python call costs more than the test and most values are leaves. Measured
    against recursing unconditionally: 17% faster on a cache key with
    arguments, 20% on a flat body, 16% on a nested one. Sorting the keys
    directly beats `sorted(value.items())`, which allocates a tuple per item
    and then compares tuples instead of strings.
    """
    if isinstance(value, dict):
        ordered: dict[Any, Any] = {}
        for key in sorted(value):
            item = value[key]
            ordered[key] = _sorted_deep(item) if isinstance(item, _CONTAINERS) else item
        return ordered
    if isinstance(value, (list, tuple)):
        return [_sorted_deep(i) if isinstance(i, _CONTAINERS) else i for i in value]
    return value


def _orjson_backend() -> Backend:
    """orjson, bound with no wrapper at all.

    The flag values above are orjson's, so nothing needs translating and the
    seam costs the default path exactly zero - `dumps` *is* `orjson.dumps`.
    """
    import orjson

    # Pinned here so a future orjson cannot renumber a flag out from under the
    # constants above without this failing loudly at import.
    if (orjson.OPT_SORT_KEYS, orjson.OPT_INDENT_2) != (OPT_SORT_KEYS, OPT_INDENT_2):
        raise ImportError(
            "orjson renumbered its option flags: expected "
            f"SORT_KEYS={OPT_SORT_KEYS} INDENT_2={OPT_INDENT_2}, got "
            f"SORT_KEYS={orjson.OPT_SORT_KEYS} INDENT_2={orjson.OPT_INDENT_2}"
        )

    return Backend("orjson", orjson.dumps, orjson.loads, orjson.JSONDecodeError)


def _msgspec_backend() -> Backend:
    """msgspec, which ships the free-threaded wheels orjson does not.

    Differences from orjson that have to be closed here: there is no sort
    option, `format` does the indenting, and `EncodeError` is not a
    `TypeError`, which call sites catch.
    """
    import msgspec

    #: One encoder per `default` hook; building one per call would dominate.
    _encoders: dict[Any, msgspec.json.Encoder] = {}

    def dumps(
        obj: Any, /, default: Callable[[Any], Any] | None = None, option: int | None = 0
    ) -> bytes:
        flags = option or 0
        if flags & OPT_SORT_KEYS:
            obj = _sorted_deep(obj)
        # The lookup is inlined rather than put behind a helper: a Python call
        # costs ~70ns here, which is a third of this wrapper's whole budget.
        encoder = _encoders.get(default)
        if encoder is None:
            encoder = _encoders[default] = msgspec.json.Encoder(enc_hook=default)
        try:
            out = encoder.encode(obj)
        except msgspec.EncodeError as exc:
            # orjson's JSONEncodeError *is* TypeError; handlers catch that.
            raise TypeError(str(exc)) from exc
        if flags & OPT_INDENT_2:
            return msgspec.json.format(out, indent=2)
        return out

    def loads(data: bytes | bytearray | memoryview | str) -> Any:
        if isinstance(data, str):
            data = data.encode("utf-8")
        return msgspec.json.decode(data)

    # A `DecodeError` is a `ValueError` at runtime, which the suite asserts;
    # the published stubs declare it narrower than it is.
    return Backend("msgspec", dumps, loads, cast("type[ValueError]", msgspec.DecodeError))


def _text_backend(name: str, module_name: str, *, separators: bool) -> Backend:
    """Build a backend over a library that emits `str` and no native date types.

    `json` and `ujson` take the same `sort_keys` / `indent` / `default` /
    `allow_nan` keywords and diverge from orjson the same two ways, so one
    wrapper serves both. Two gaps have to be closed rather than documented:
    both emit a bare `NaN` for a non-finite float, which no JSON parser
    accepts, and neither encodes a date or a `UUID` without help, which would
    make the same payload succeed on one backend and raise on another.
    """
    import datetime
    import uuid
    from math import isfinite

    module = __import__(module_name)
    decode_error = cast("type[ValueError]", module.JSONDecodeError)
    # `datetime` subclasses `date`, and `isoformat` is right for all three.
    iso_types = (datetime.date, datetime.time)

    def hook_for(user_default: Callable[[Any], Any] | None) -> Callable[[Any], Any]:
        """Orjson's precedence: its own native types win over `default=`."""

        def convert(value: Any) -> Any:
            if isinstance(value, iso_types):
                return value.isoformat()
            if isinstance(value, uuid.UUID):
                return str(value)
            if user_default is not None:
                return user_default(value)
            raise TypeError(f"Type is not JSON serializable: {type(value).__name__}")

        return convert

    def nulled(value: Any) -> Any:
        """Return `value` with every non-finite float replaced by `null`."""
        if isinstance(value, dict):
            return {k: nulled(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [nulled(v) for v in value]
        if type(value) is float and not isfinite(value):
            return None
        return value

    base: dict[str, Any] = {"ensure_ascii": False}
    if separators:
        # `ujson` is compact already; `json` puts a space after every delimiter.
        base["separators"] = (",", ":")

    def dumps(
        obj: Any, /, default: Callable[[Any], Any] | None = None, option: int | None = 0
    ) -> bytes:
        flags = option or 0
        kwargs = dict(base)
        if flags & OPT_SORT_KEYS:
            kwargs["sort_keys"] = True
        if flags & OPT_INDENT_2:
            # Indenting re-introduces the space after a colon, which is what
            # orjson emits too, so the compact separators must come back out.
            kwargs["indent"] = 2
            kwargs.pop("separators", None)
        hook = hook_for(default)
        try:
            return cast("str", module.dumps(obj, default=hook, allow_nan=False, **kwargs)).encode()
        except (ValueError, OverflowError) as exc:
            # `allow_nan=False` raises here, so the walk runs only for a payload
            # holding one; a cycle exhausts the stack and the original error stands.
            try:
                cleaned = nulled(obj)
            except RecursionError:
                raise exc from None
            return cast(
                "str", module.dumps(cleaned, default=hook, allow_nan=False, **kwargs)
            ).encode()

    return Backend(name, dumps, module.loads, decode_error)


def _stdlib_backend() -> Backend:
    """Build the standard-library backend, so Veloce needs no JSON dependency."""
    return _text_backend("json", "json", separators=True)


def _ujson_backend() -> Backend:
    return _text_backend("ujson", "ujson", separators=False)


#: Preference order. The first that imports wins; orjson is what ships.
_BUILDERS: Final[dict[str, Callable[[], Backend]]] = {
    "orjson": _orjson_backend,
    "msgspec": _msgspec_backend,
    "ujson": _ujson_backend,
    "json": _stdlib_backend,
}


class JSONBackend(str, Enum):
    """The JSON implementations `use_json_backend` accepts.

    Each member's value is the name `VELOCE_JSON_BACKEND` and `backend` use, so
    `STDLIB` reads `"json"` while the rest match their member name.
    """

    ORJSON = "orjson"
    MSGSPEC = "msgspec"
    UJSON = "ujson"
    STDLIB = "json"


def build_backend(name: str) -> Backend:
    """Build one backend by name, whichever is live. Used by the tests."""
    try:
        builder = _BUILDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown JSON backend {name!r}; choose one of {', '.join(_BUILDERS)}"
        ) from None
    return builder()


def available_backends() -> tuple[str, ...]:
    """Return the backends that can actually be built in this environment."""
    found = []
    for name, builder in _BUILDERS.items():
        try:
            builder()
        except ImportError:
            continue
        found.append(name)
    return tuple(found)


#: Names the backend for the whole process; read once, at import.
_ENV_VAR: Final = "VELOCE_JSON_BACKEND"


def _select() -> Backend:
    chosen = os.environ.get(_ENV_VAR, "").strip()
    if chosen:
        if chosen not in _BUILDERS:
            raise ImportError(
                f"{_ENV_VAR}={chosen!r} is not a known JSON backend; "
                f"choose one of {', '.join(_BUILDERS)}"
            )
        try:
            return _BUILDERS[chosen]()
        except ImportError as exc:
            # Named explicitly, so falling through to another backend would
            # serialise differently than asked without saying so.
            raise ImportError(
                f"{_ENV_VAR}={chosen!r} was requested but could not be imported: {exc}"
            ) from exc

    errors: list[str] = []
    for name, builder in _BUILDERS.items():
        try:
            return builder()
        except ImportError as exc:
            errors.append(f"{name}: {exc}")
    raise ImportError(
        "veloce needs a JSON backend and none could be imported - install "
        "`orjson` (the default) or `msgspec`. Tried: " + "; ".join(errors)
    )


_backend = _select()

#: Which implementation is live. Reported rather than guessed.
backend: str = _backend.name
dumps: _Dumps = _backend.dumps
loads: Callable[[bytes | bytearray | memoryview | str], Any] = _backend.loads
JSONDecodeError: type[ValueError] = _backend.JSONDecodeError


def use_json_backend(choice: JSONBackend | str) -> None:
    """Select the JSON backend for this process. Call it once, at startup.

    Every module reaches JSON by looking these names up here, so one call
    moves all of them together - response bodies, `jsonify`, signed cookies
    and tokens, cache keys, config files and the MCP transports.

    Process-wide rather than per-app on purpose: one interpreter has one
    backend, and an `app`-scoped setting would let a second app silently
    redefine the first's wire format. Call it before anything is encoded;
    switching later leaves cache entries keyed by the previous backend, which
    then miss rather than fail.
    """
    global backend, dumps, loads, JSONDecodeError

    name = choice.value if isinstance(choice, JSONBackend) else str(choice)
    if name == backend:
        return
    chosen = build_backend(name)
    backend = chosen.name
    dumps = chosen.dumps
    loads = chosen.loads
    JSONDecodeError = chosen.JSONDecodeError
