"""Pluggable JSON provider.

The base `JSONProvider` declares three methods: `dumps`/`loads` for the
bytes <-> object boundary, and `response` for handing a
JSON-serialisable value to a `Response` ready to send. Veloce ships a
default `DefaultJSONProvider` backed by orjson for speed, plus a
`JSONResponseProvider` shim that wraps `JSONResponse` construction so
callers can override the response class without rewriting the encoder.

Custom providers subclass `JSONProvider`; the app picks one by setting
`app.json` to an instance (or pointing `app.json_provider_class` at a
class, instantiated lazily).
"""

from __future__ import annotations

from typing import Any

import orjson


class JSONProvider:
    """Base class for JSON serialisation providers."""

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
        """Build a `Response` carrying `value` as JSON. Default delegates
        to `dumps` + a `JSONResponse`."""
        from veloce.http.response import JSONResponse, Response

        body = self.dumps(value)
        # `JSONResponse.__new__` skips its default orjson re-encoding so
        # caller-provided options (e.g. sort_keys) survive.
        resp = JSONResponse.__new__(JSONResponse)
        Response.__init__(
            resp,
            status_code=int(kwargs.pop("status_code", 200)),
            body=body,
            content_type="application/json",
            headers=kwargs.pop("headers", None),
        )
        return resp


class DefaultJSONProvider(JSONProvider):
    """orjson-backed provider — Veloce's default.

    Honours two `app.config` flags so the existing `JSON_SORT_KEYS` /
    `JSONIFY_PRETTYPRINT_REGULAR` toggles keep working without callers
    needing to subclass.
    """

    def dumps(self, obj: Any, **kwargs: Any) -> bytes:
        opts = 0
        cfg = getattr(self._app, "config", None)
        if cfg is not None:
            if cfg.get("JSON_SORT_KEYS") or kwargs.get("sort_keys"):
                opts |= orjson.OPT_SORT_KEYS
            if cfg.get("JSONIFY_PRETTYPRINT_REGULAR") or kwargs.get("indent"):
                opts |= orjson.OPT_INDENT_2
        return orjson.dumps(obj, option=opts) if opts else orjson.dumps(obj)

    def loads(self, data: bytes | str) -> Any:
        return orjson.loads(data)
