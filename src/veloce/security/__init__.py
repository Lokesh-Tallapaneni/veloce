"""Security sub-package — authentication schemes for dependency injection."""

from __future__ import annotations

from veloce.security.api_key import APIKeyCookie, APIKeyHeader, APIKeyQuery
from veloce.security.base import SecurityScheme
from veloce.security.http import (
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
    HTTPDigest,
    HTTPDigestCredentials,
)
from veloce.security.jwt import (
    Claims,
    ExpiredSignatureError,
    ImmatureSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    InvalidTokenError,
    JWTError,
    MissingClaimError,
    UnsupportedAlgorithmError,
    decode_jwt,
    encode_jwt,
)
from veloce.security.oauth2 import (
    OAuth2AuthorizationCodeBearer,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
    OAuth2PasswordRequestFormStrict,
    OpenIdConnect,
)
from veloce.security.reset_token import (
    BadResetToken,
    check_reset_token,
    make_reset_token,
)
from veloce.security.session import SessionAuth, login_session, logout_session

__all__ = [
    "SessionAuth",
    "login_session",
    "logout_session",
    "SecurityScheme",
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
    "Claims",
    "JWTError",
    "InvalidTokenError",
    "InvalidSignatureError",
    "UnsupportedAlgorithmError",
    "ExpiredSignatureError",
    "ImmatureSignatureError",
    "InvalidAudienceError",
    "InvalidIssuerError",
    "MissingClaimError",
    "encode_jwt",
    "decode_jwt",
    "BadResetToken",
    "make_reset_token",
    "check_reset_token",
]
