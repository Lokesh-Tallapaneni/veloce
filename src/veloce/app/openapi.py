"""OpenAPI setup — schema and docs-page wiring, mixed into Veloce.

Holds the lazy schema build (`openapi`) and the route registration that wires the
JSON schema endpoint and the Swagger / ReDoc UIs (`_setup_openapi`). A mixin on
`Veloce`; both run off the request path (schema on first access, routes once at
first-request setup). The actual schema generation and UI routes live in
`veloce.contrib.openapi` and are imported lazily so an app that disables OpenAPI
pays nothing for it.
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
        self._openapi_setup = True
        if self._openapi_url:
            # Deferred: `contrib/` is optional.
            from veloce.contrib.docs_ui import setup_openapi_routes

            # Pass the configured URLs through unchanged - `None` means
            # "do not register that UI", and must not be replaced by a
            # default path.
            setup_openapi_routes(
                self,
                openapi_url=self._openapi_url,
                docs_url=self._docs_url,
                redoc_url=self._redoc_url,
            )

    def openapi(self) -> dict[str, Any]:
        """Return the generated OpenAPI schema dict.

        Computes the schema on first call, caches the result in
        `app.openapi_schema`. Subsequent calls return the cached dict;
        users can mutate the result in place (e.g. to inject custom
        `info.x-logo` or `tags` orderings) and the swagger UI / json
        endpoints will serve the mutated copy.

        To bypass the auto-build entirely, assign a custom dict to
        `app.openapi_schema` before any request lands.
        """
        if self.openapi_schema is None:
            # Deferred: `contrib/` is optional.
            from veloce.contrib.openapi import get_openapi_schema

            self.openapi_schema = get_openapi_schema(self)
        return self.openapi_schema
