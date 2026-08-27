"""JSON provider — pluggable serialisation boundary for response bodies.

The base `JSONProvider` declares three methods: `dumps`/`loads` for the
bytes <-> object boundary, and `response` for handing a
JSON-serialisable value to a `Response` ready to send. Veloce ships a
default `DefaultJSONProvider` backed by orjson for speed.

Custom providers subclass `JSONProvider`; the app picks one by setting
`app.json` to an instance (or pointing `app.json_provider_class` at a
class, instantiated lazily).
"""

from __future__ import annotations

from typing import Any

import orjson

from veloce._internal import _require_methods
from veloce.encoders import orjson_default
from veloce.http.response import JSONResponse
from veloce.status import HTTP_200_OK


def config_orjson_options(cfg: Any) -> int:
    """Build the orjson option bitmask from an app config mapping.

    Reads the `JSON_SORT_KEYS` and `JSONIFY_PRETTYPRINT_REGULAR` flags.
    Shared by `DefaultJSONProvider` and `helpers.jsonify` so the two
    paths cannot drift. Returns `0` when `cfg` is `None`.
    """
    opts = 0
    if cfg is not None:
        if cfg.get("JSON_SORT_KEYS"):
            opts |= orjson.OPT_SORT_KEYS
        if cfg.get("JSONIFY_PRETTYPRINT_REGULAR"):
            opts |= orjson.OPT_INDENT_2
    return opts


class JSONProvider:
    """Base class for JSON serialisation providers.

    Subclass to plug in an alternative serialiser, then point the app at it
    via `app.json` (an instance) or `app.json_provider_class` (a class,
    instantiated lazily on first access).

    Usage::

        class MyJSONProvider(JSONProvider):
            def dumps(self, obj, **kwargs):
                return my_lib.dumps(obj).encode()

            def loads(self, data):
                return my_lib.loads(data)

        app.json_provider_class = MyJSONProvider
    """

    #: Both halves a provider must supply, checked at definition rather than
    #: left to fail at call time - a provider missing one of them works until
    #: the first response that needs to encode, or the first request body that
    #: needs to decode, and then fails on a live request.
    _required = ("dumps", "loads")

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _require_methods(cls, JSONProvider, JSONProvider._required)

    def __init__(self, app: Any) -> None:
        self._app = app

    def dumps(self, obj: Any, **kwargs: Any) -> bytes:
        """Serialise `obj` to JSON bytes. Subclasses override.

        Returns `bytes` (not `str`) so callers can write directly to a
        response body without re-encoding. The kwargs catch-all is
        provider-specific (e.g. `indent=2`, `sort_keys=True`).
        """
        raise NotImplementedError

    def loads(self, data: bytes | str) -> Any:
        """Parse JSON `data` into Python objects."""
        raise NotImplementedError

    def response(self, value: Any, **kwargs: Any) -> Any:
        """Build a `Response` carrying `value` as JSON.

        The default implementation delegates to `dumps` and wraps the result
        in a `JSONResponse`.
        """
        # `from_bytes` skips JSONResponse's default re-encode so
        # caller-provided dumps options (e.g. sort_keys) survive.
        return JSONResponse.from_bytes(
            self.dumps(value),
            status_code=int(kwargs.pop("status_code", HTTP_200_OK)),
            headers=kwargs.pop("headers", None),
        )


class DefaultJSONProvider(JSONProvider):
    """orjson-backed provider — Veloce's default.

    Honours two `app.config` flags so the existing `JSON_SORT_KEYS` /
    `JSONIFY_PRETTYPRINT_REGULAR` toggles keep working without callers
    needing to subclass.
    """

    def __init__(self, app: Any) -> None:
        super().__init__(app)
        # The `JSON_SORT_KEYS` / `JSONIFY_PRETTYPRINT_REGULAR` flags are read
        # once here and cached as a bitmask: the provider is instantiated lazily
        # on first `app.json` access, by which point startup config is settled.
        # Mutating those flags afterwards does not retroactively change this
        # provider; set them before the first `app.json` access. Per-call
        # `sort_keys` / `indent` overrides are still ORed in on each `dumps`.
        self._config_options = config_orjson_options(getattr(app, "config", None))

    def dumps(self, obj: Any, **kwargs: Any) -> bytes:
        opts = self._config_options
        if kwargs.get("sort_keys"):
            opts |= orjson.OPT_SORT_KEYS
        if kwargs.get("indent"):
            opts |= orjson.OPT_INDENT_2
        # `default=` is only invoked by orjson for leaf types it cannot encode
        # itself, so the common path keeps orjson's C-speed traversal at zero
        # added cost while set/Path/Decimal/custom objects serialise instead of
        # raising `TypeError`.
        if opts:
            return orjson.dumps(obj, option=opts, default=orjson_default)
        return orjson.dumps(obj, default=orjson_default)

    def loads(self, data: bytes | str) -> Any:
        return orjson.loads(data)


# `dumps_for` / `dumps_current` live in `_internal`, so `http.response` - the
# lower layer - can reach the one encode funnel without importing this module.
# `resolve_dumps` stays here because it answers a question only a provider can:
# whether this app configured one at all.


def resolve_dumps(app: Any) -> Any:
    """`app`'s serialiser, or `None` when the direct encoder already matches it.

    Separate from `dumps_for` because it answers a different question: not "encode
    this" but "is there anything to do differently?". `None` says the stock
    provider with nothing configured is active, which emits exactly what the
    direct call does - letting the dispatch path skip the indirection for an
    application that configured nothing.
    """
    provider = app.json
    if type(provider) is DefaultJSONProvider and not provider._config_options:
        return None
    return provider.dumps
