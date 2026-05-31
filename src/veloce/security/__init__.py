"""Security sub-package - authentication schemes for dependency injection."""

from __future__ import annotations

from veloce.security.api_key import APIKeyCookie, APIKeyHeader, APIKeyQuery
from veloce.security.http import (
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
    HTTPDigest,
    HTTPDigestCredentials,
)
from veloce.security.oauth2 import (
    OAuth2AuthorizationCodeBearer,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
    OAuth2PasswordRequestFormStrict,
    OpenIdConnect,
)

__all__ = [
    "HTTPBasic",
    "HTTPBasicCredentials",
    "HTTPBearer",
    "HTTPDigest",
    "HTTPDigestCredentials",
    "APIKeyHeader",
    "APIKeyQuery",
    "APIKeyCookie",
    "OAuth2PasswordBearer",
    "OAuth2PasswordRequestForm",
    "OAuth2PasswordRequestFormStrict",
    "OAuth2AuthorizationCodeBearer",
    "OpenIdConnect",
]
