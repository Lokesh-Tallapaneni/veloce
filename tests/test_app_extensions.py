"""app.extensions — the registry dict for third-party extensions."""

from __future__ import annotations

from veloce import Veloce


class TestExtensions:
    def test_extensions_registry(self):
        app = Veloce(openapi_url=None)
        app.extensions["cache"] = {"type": "redis", "url": "redis://localhost"}
        assert app.extensions["cache"]["type"] == "redis"
