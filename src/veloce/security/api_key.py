"""API Key authentication schemes — header, query, cookie."""

from __future__ import annotations

from typing import Any

from veloce._constants import HEADER_WWW_AUTHENTICATE
from veloce.http.request import Request
from veloce.security._utils import _extract_api_key, _quote_header_value, _validate_realm

# RFC 9110 Sec. 11.6.1 - the auth-scheme token for the API-key 401 challenge.
# Not an IANA-registered scheme, but a bare `WWW-Authenticate: APIKey` (or
# with a quoted `realm`) is a valid challenge and tells clients which scheme
# the resource expects.
_APIKEY_SCHEME = "APIKey"


class _APIKeyBase:
    """Shared logic for `APIKeyHeader`, `APIKeyQuery`, `APIKeyCookie`.

    Each subclass differs only in which `Request` collection it pulls
    the key from. The `__init__` (store `name` + `auto_error` + `realm`)
    and the delegation to `_extract_api_key` were three copies of the
    same lines; centralising prevents the three from drifting apart on a
    future change to the extraction signature.
    """

    _source_attr: str = ""  # subclass overrides - Request attribute name
    __slots__ = ("name", "auto_error", "realm", "_challenge")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls._source_attr:
            raise TypeError(f"{cls.__name__} must set _source_attr to a Request attribute name")

    def __init__(self, name: str, auto_error: bool = True, realm: str = "") -> None:
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
        """The `WWW-Authenticate` challenge sent on a 401.

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


class APIKeyHeader(_APIKeyBase):
    """API Key authentication via HTTP header."""

    __slots__ = ()
    _source_attr = "headers"


class APIKeyQuery(_APIKeyBase):
    """API Key authentication via query parameter."""

    __slots__ = ()
    _source_attr = "query_params"


class APIKeyCookie(_APIKeyBase):
    """API Key authentication via cookie."""

    __slots__ = ()
    _source_attr = "cookies"
