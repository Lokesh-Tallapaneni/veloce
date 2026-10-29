"""OpenAPI setup — schema and docs-page wiring, mixed into Veloce.

Both entry points run off the request path, and import `veloce.contrib.openapi`
lazily so an app that disables OpenAPI pays nothing for it.
"""

from __future__ import annotations

from typing import Any

from veloce.app._host import AppHost


class OpenAPIMixin(AppHost):
    """OpenAPI schema build and docs-route registration, mixed into `Veloce`."""

    def _setup_openapi(self) -> None:
        """Register OpenAPI/Swagger routes if enabled."""
        if self._openapi_setup:
            return
        # Registration writes the route table, and the flag below is what
        # another event loop reads to decide it has finished - so it is set
        # last, and under a guard, or that loop dispatches against a table
        # still being written.
        with self._openapi_setup_guard:
            if self._openapi_setup:
                return
            if self._openapi_url:
                # Deferred: `contrib/` is optional.
                from veloce.contrib.docs_ui import setup_openapi_routes

                # A `None` URL means "do not register that UI" - never defaulted.
                setup_openapi_routes(
                    self,
                    openapi_url=self._openapi_url,
                    docs_url=self._docs_url,
                    redoc_url=self._redoc_url,
                )
            self._openapi_setup = True

    def openapi(self) -> dict[str, Any]:
        """Return the generated OpenAPI schema dict.

        Cached in `app.openapi_schema`; mutating the returned dict in place
        changes what the UI and JSON endpoints serve. Assign that attribute
        before any request lands to bypass the auto-build entirely.
        """
        if self.openapi_schema is None:
            # Deferred: `contrib/` is optional.
            from veloce.contrib.openapi import get_openapi_schema

            self.openapi_schema = get_openapi_schema(self)
        return self.openapi_schema
