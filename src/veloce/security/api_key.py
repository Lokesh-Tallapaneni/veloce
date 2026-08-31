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
# Not an IANA-registered scheme, but a bare `WWW-Authenticate: APIKey` (or
# with a quoted `realm`) is a valid challenge and tells clients which scheme
# the resource expects.
_APIKEY_SCHEME = "APIKey"


class _APIKeyBase(SecurityScheme):
    """Shared logic for `APIKeyHeader`, `APIKeyQuery`, `APIKeyCookie`.

    Each subclass differs only in which `Request` collection it pulls
    the key from. The `__init__` (store `name` + `auto_error` + `realm`)
    and the delegation to `_extract_api_key` were three copies of the
    same lines; centralising prevents the three from drifting apart on a
    future change to the extraction signature.
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
        # Paired with `_source_attr`: a subclass that reads a location OpenAPI
        # cannot name would publish a scheme object with an empty `in`, which
        # is what silent under-description looked like before.
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
        # Keep the user's casing for the OpenAPI spec; header lookup goes
        # through the case-insensitive `Headers` (CIMultiDict) so the case
        # doesn't matter at read time.
        _validate_realm(realm)
        self.name = name
        self.auto_error = auto_error
        self.realm = realm
        # The challenge is request-invariant, so build it once at
        # construction and read a single attribute per request.
        self._challenge = self.challenge()

    def challenge(self) -> dict[str, str]:
        """Build the `WWW-Authenticate` challenge sent on a 401.

        Returns `{WWW-Authenticate: APIKey realm="..."}` when a realm is
        configured, else the bare `APIKey` token, which still satisfies
        RFC 9110 Sec. 11.6.1. Subclasses may override to emit a custom
        challenge.
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
        # Read through the connection's own single-header accessor rather than
        # its header collection. On a WebSocket route that collection is a
        # plain lowercase-keyed dict, so a canonically-cased `name` such as
        # `X-API-Key` missed it and every key looked absent; the accessor is
        # case-insensitive on both transports, and on the HTTP one it answers
        # without building the whole mapping.
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
