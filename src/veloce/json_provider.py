"""JSON provider — pluggable serialisation boundary for response bodies.

`JSONProvider` declares `dumps`/`loads` for the bytes <-> object boundary and
`response` for handing a value to a `Response`. An app picks one by setting
`app.json` to an instance, or `app.json_provider_class` to a class.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import veloce._json as _json
from veloce._internal import _require_methods
from veloce.encoders import orjson_default
from veloce.http.response import JSONResponse, Response
from veloce.status import HTTP_200_OK


def config_orjson_options(cfg: Any) -> int:
    """Build the JSON option bitmask from an app config mapping.

    Reads `JSON_SORT_KEYS` and `JSONIFY_PRETTYPRINT_REGULAR`; `0` when `cfg`
    is `None`.
    """
    opts = 0
    if cfg is not None:
        if cfg.get("JSON_SORT_KEYS"):
            opts |= _json.OPT_SORT_KEYS
        if cfg.get("JSONIFY_PRETTYPRINT_REGULAR"):
            opts |= _json.OPT_INDENT_2
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

    #: Checked at definition: one missing either works until a live request.
    _required = ("dumps", "loads")

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _require_methods(cls, JSONProvider, JSONProvider._required)

    def __init__(self, app: Any) -> None:
        self._app = app

    def dumps(self, obj: Any, **kwargs: Any) -> bytes:
        """Serialise `obj` to JSON bytes. Subclasses override.

        `bytes`, not `str`, so a caller can write straight to a response body.
        The kwargs catch-all is provider-specific.
        """
        raise NotImplementedError

    def loads(self, data: bytes | str) -> Any:
        """Parse JSON `data` into Python objects."""
        raise NotImplementedError

    def response(self, value: Any, **kwargs: Any) -> Response:
        """Build a `Response` carrying `value` as JSON."""
        # `from_bytes` skips the re-encode, so caller dumps options survive.
        return JSONResponse.from_bytes(
            self.dumps(value),
            status_code=int(kwargs.pop("status_code", HTTP_200_OK)),
            headers=kwargs.pop("headers", None),
        )


class DefaultJSONProvider(JSONProvider):
    """Provider over the configured JSON backend — Veloce's default.

    Honours the `JSON_SORT_KEYS` / `JSONIFY_PRETTYPRINT_REGULAR` config flags,
    so neither needs a subclass.
    """

    def __init__(self, app: Any) -> None:
        super().__init__(app)
        # Cached: the provider is built on first `app.json` access, so mutating
        # the flags afterwards does not change it.
        self._config_options = config_orjson_options(getattr(app, "config", None))

    def dumps(self, obj: Any, **kwargs: Any) -> bytes:
        opts = self._config_options
        if kwargs.get("sort_keys"):
            opts |= _json.OPT_SORT_KEYS
        if kwargs.get("indent"):
            opts |= _json.OPT_INDENT_2
        # The backend calls `default=` only for leaf types it cannot encode
        # itself, so the common path keeps its C traversal at no added cost.
        if opts:
            return _json.dumps(obj, option=opts, default=orjson_default)
        return _json.dumps(obj, default=orjson_default)

    def loads(self, data: bytes | str) -> Any:
        return _json.loads(data)


# `dumps_for` / `dumps_current` live in `_internal`, so `http.response` reaches
# the one encode funnel without importing this module.


def resolve_dumps(app: Any) -> Callable[..., bytes] | None:
    """`app`'s serialiser, or `None` when the direct encoder already matches it.

    `None` says the stock provider with nothing configured is active, so the
    dispatch path can skip the indirection entirely.
    """
    provider = app.json
    if type(provider) is DefaultJSONProvider and not provider._config_options:
        return None
    dumps: Callable[..., bytes] = provider.dumps
    return dumps
