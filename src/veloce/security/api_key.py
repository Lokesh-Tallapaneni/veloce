"""API Key authentication schemes — header, query, cookie."""

from __future__ import annotations

from typing import Annotated, Any

from typing_extensions import Doc

from veloce._constants import HEADER_WWW_AUTHENTICATE
from veloce.http.request import Request
from veloce.security._utils import (
    _extract_api_key,
    _quote_header_value,
    _refuse_missing_api_key,
    _validate_realm,
)
from veloce.security.base import SecurityScheme

# RFC 9110 Sec. 11.6.1 - the auth-scheme token for the API-key 401 challenge.
# Not IANA-registered, but a bare `APIKey` is a valid challenge.
_APIKEY_SCHEME = "APIKey"


class _APIKeyBase(SecurityScheme):
    """Shared logic for `APIKeyHeader`, `APIKeyQuery`, `APIKeyCookie`.

    Each subclass differs only in which `Request` collection it pulls the key
    from.
    """

    _source_attr: str = ""  # subclass overrides - Request attribute name
    #: The OpenAPI `in` location the subclass reads the key from.
    _openapi_in: str = ""
    # `auto_error` is owned by `SecurityScheme`'s slots.
    __slots__ = ("name", "realm", "_challenge")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls._source_attr:
            raise TypeError(f"{cls.__name__} must set _source_attr to a Request attribute name")
        # Without it the published scheme object would carry an empty `in`.
        if not cls._openapi_in:
            raise TypeError(f"{cls.__name__} must set _openapi_in to an OpenAPI 'in' location")

    def __init__(
        self,
        name: Annotated[
            str,
            Doc("Name of the header, query parameter or cookie carrying the key."),
        ],
        auto_error: Annotated[
            bool,
            Doc("Raise 401 when the key is absent; False resolves to None instead."),
        ] = True,
        realm: Annotated[
            str,
            Doc("Realm published in the `WWW-Authenticate` challenge on a 401."),
        ] = "",
    ) -> None:
        # `name` keeps the user's casing for the OpenAPI spec; lookups are
        # case-insensitive, so the case does not matter at read time.
        _validate_realm(realm)
        self.name = name
        self.auto_error = auto_error
        self.realm = realm
        # Request-invariant, so build it once and read one attribute per request.
        self._challenge = self.challenge()

    def challenge(self) -> dict[str, str]:
        """Build the `WWW-Authenticate` challenge sent on a 401.

        A bare `APIKey` token when no realm is configured, which still satisfies
        RFC 9110 Sec. 11.6.1. Subclasses may override it.
        """
        if self.realm:
            value = f'{_APIKEY_SCHEME} realm="{_quote_header_value(self.realm)}"'
        else:
            value = _APIKEY_SCHEME
        return {HEADER_WWW_AUTHENTICATE: value}

    def __call__(self, request: Request) -> str | None:
        source: Any = getattr(request, self._source_attr)
        return _extract_api_key(source, self.name, self.auto_error, self._challenge)

    def openapi_scheme(self) -> dict[str, Any] | None:
        """Describe an API key, in the location this subclass reads it from."""
        return {"type": "apiKey", "in": self._openapi_in, "name": self.name}


class APIKeyHeader(_APIKeyBase):
    """API Key authentication via HTTP header."""

    __slots__ = ()
    _source_attr = "headers"
    _openapi_in = "header"

    def __call__(self, request: Request) -> str | None:
        # Not the header collection: on a WebSocket route that is a plain
        # lowercase-keyed dict, which a canonically-cased `name` would miss.
        key = request._peek_header(self.name)
        return _refuse_missing_api_key(key, self.auto_error, self._challenge)


class APIKeyQuery(_APIKeyBase):
    """API Key authentication via query parameter."""

    __slots__ = ()
    _source_attr = "query_params"
    _openapi_in = "query"


class APIKeyCookie(_APIKeyBase):
    """API Key authentication via cookie."""

    __slots__ = ()
    _source_attr = "cookies"
    _openapi_in = "cookie"
