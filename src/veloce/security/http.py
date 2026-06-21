"""HTTP authentication schemes — Basic, Digest, Bearer."""

from __future__ import annotations

import base64
import binascii
import secrets
from typing import Any

from veloce._constants import HEADER_AUTHORIZATION, HEADER_WWW_AUTHENTICATE, MSG_NOT_AUTHENTICATED
from veloce._header_parsing import parse_header_params
from veloce._protocol_constants import AUTH_SCHEME_BASIC, AUTH_SCHEME_BEARER, AUTH_SCHEME_DIGEST
from veloce.exceptions import HTTPException
from veloce.http.request import Request
from veloce.security._utils import _quote_header_value, _validate_realm
from veloce.security.base import SecurityScheme, _BearerScheme
from veloce.status import HTTP_401_UNAUTHORIZED

# `_quote_header_value` / `_validate_realm` now live in `_utils` so the
# API-key schemes can share them without an import cycle; they remain
# importable from `veloce.security.http` and are used by the schemes below.
_BASIC_PREFIX = (AUTH_SCHEME_BASIC + " ").lower()
_BASIC_PREFIX_LEN = len(_BASIC_PREFIX)
_DIGEST_PREFIX = (AUTH_SCHEME_DIGEST + " ").lower()
_DIGEST_PREFIX_LEN = len(_DIGEST_PREFIX)


class HTTPBasicCredentials:
    """HTTP Basic auth credentials."""

    __slots__ = ("username", "password")

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password


class HTTPBasic(SecurityScheme):
    """HTTP Basic authentication - extracts username:password from Authorization header."""

    # `auto_error` is owned by `SecurityScheme`'s slots.
    __slots__ = ("realm", "_challenge_template")

    def __init__(self, auto_error: bool = True, realm: str = "") -> None:
        _validate_realm(realm)
        self.auto_error = auto_error
        self.realm = realm
        # The `WWW-Authenticate: Basic realm="..."` challenge (or `{}` when no
        # realm is configured) is request-invariant, so build it once at
        # construction as a template. Each 401 raise copies it via
        # `_challenge()` so response middleware (CORS/Session/SecurityHeaders)
        # mutating `response.headers` in place cannot leak request-specific
        # headers (Vary, Set-Cookie, Access-Control-*) into the shared dict and
        # carry them onto every subsequent challenge.
        if realm:
            self._challenge_template = {
                HEADER_WWW_AUTHENTICATE: f'{AUTH_SCHEME_BASIC} realm="{_quote_header_value(realm)}"'
            }
        else:
            self._challenge_template = {}

    def _challenge(self) -> dict[str, str]:
        # Fresh copy per 401 so each challenge response is isolated from the
        # next; the template stays immutable.
        return dict(self._challenge_template)

    def __call__(self, request: Request) -> HTTPBasicCredentials | None:
        auth = request.headers.get(HEADER_AUTHORIZATION, "")
        if auth[:_BASIC_PREFIX_LEN].lower() != _BASIC_PREFIX:
            if self.auto_error:
                raise HTTPException(
                    HTTP_401_UNAUTHORIZED,
                    MSG_NOT_AUTHENTICATED,
                    headers=self._challenge(),
                )
            return None

        # Catch only the exceptions that `b64decode(validate=True)` and
        # the subsequent `decode("utf-8")` can raise - `binascii.Error`
        # / `ValueError` from base64 and `UnicodeDecodeError` from the
        # text conversion. A bare `except Exception` would also swallow
        # genuine bugs (NameError, AttributeError) and convert them to
        # a 401, masking defects.
        try:
            decoded = base64.b64decode(auth[_BASIC_PREFIX_LEN:], validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as err:
            raise HTTPException(
                HTTP_401_UNAUTHORIZED,
                "Invalid authentication credentials",
                headers=self._challenge(),
            ) from err
        # RFC 7617 Sec. 2: the credentials are `userid ":" password`; the colon
        # is mandatory. A colon-less payload is malformed and must not
        # authenticate (it would otherwise pass as an empty-password login).
        username, sep, password = decoded.partition(":")
        if not sep:
            raise HTTPException(
                HTTP_401_UNAUTHORIZED,
                "Invalid authentication credentials",
                headers=self._challenge(),
            )
        return HTTPBasicCredentials(username=username, password=password)


class HTTPDigestCredentials:
    """Parsed Digest auth challenge response - RFC 7616 Sec. 3.4."""

    __slots__ = (
        "username",
        "realm",
        "nonce",
        "uri",
        "response",
        "qop",
        "nc",
        "cnonce",
        "opaque",
        "algorithm",
    )

    def __init__(
        self,
        username: str = "",
        realm: str = "",
        nonce: str = "",
        uri: str = "",
        response: str = "",
        qop: str = "",
        nc: str = "",
        cnonce: str = "",
        opaque: str = "",
        algorithm: str = "",
    ) -> None:
        self.username = username
        self.realm = realm
        self.nonce = nonce
        self.uri = uri
        self.response = response
        self.qop = qop
        self.nc = nc
        self.cnonce = cnonce
        self.opaque = opaque
        self.algorithm = algorithm


class HTTPDigest(SecurityScheme):
    """HTTP Digest authentication - RFC 7616.

    Parses the `Authorization: Digest ...` header into the named fields
    and returns them as `HTTPDigestCredentials`. **This class does NOT
    validate the response hash** - the application owns the secret
    (HA1) and must compute the expected digest itself; Digest's whole
    point is that the secret never crosses the wire. Veloce's job is to
    parse the challenge response and to emit a 401 + `WWW-Authenticate:
    Digest ...` header when auth is missing or malformed.

    The scheme's responsibility is the parse + challenge dance;
    verifying the response is application logic.
    """

    # `auto_error` is owned by `SecurityScheme`'s slots.
    __slots__ = (
        "realm",
        "qop",
        "algorithm",
        "nonce_factory",
        "_challenge_prefix",
        "_challenge_suffix",
    )

    def __init__(
        self,
        realm: str,
        qop: str = "auth",
        algorithm: str = "SHA-256",
        auto_error: bool = True,
        nonce_factory: Any = None,
    ) -> None:
        # RFC 7616 Sec. 3.2 prefers SHA-256; MD5 remains accepted for back-compat
        # with RFC 2617 clients but should not be the default for new servers.
        _validate_realm(realm)
        self.realm = realm
        self.qop = qop
        self.algorithm = algorithm
        self.auto_error = auto_error
        self.nonce_factory = nonce_factory or _default_nonce
        # Only the `nonce` varies between challenges; the quoted realm, qop and
        # algorithm params are request-invariant, so precompute the constant
        # prefix and suffix once and splice the per-call nonce in between.
        self._challenge_prefix = (
            f'{AUTH_SCHEME_DIGEST} realm="{_quote_header_value(realm)}", qop="{qop}", nonce="'
        )
        self._challenge_suffix = f'", algorithm={algorithm}'

    def _challenge_headers(self) -> dict[str, str]:
        # RFC 7616 Sec. 3.3 - challenge param names case-insensitive but
        # the quoted-string values must be exact. Build the header
        # rigorously; clients in the wild reject malformed challenges.
        nonce = self.nonce_factory()
        return {HEADER_WWW_AUTHENTICATE: self._challenge_prefix + nonce + self._challenge_suffix}

    def __call__(self, request: Request) -> HTTPDigestCredentials | None:
        auth = request.headers.get(HEADER_AUTHORIZATION, "")
        if auth[:_DIGEST_PREFIX_LEN].lower() != _DIGEST_PREFIX:
            if self.auto_error:
                raise HTTPException(
                    HTTP_401_UNAUTHORIZED,
                    MSG_NOT_AUTHENTICATED,
                    headers=self._challenge_headers(),
                )
            return None
        return _parse_digest(auth[_DIGEST_PREFIX_LEN:])


def _default_nonce() -> str:
    """Generate an opaque nonce for the Digest challenge.

    16 random bytes hex-encoded - well above the 64-bit entropy floor
    RFC 7616 Sec. 5.3 recommends. Server-side nonce replay tracking is
    application territory.
    """
    return secrets.token_hex(16)


def _parse_digest(value: str) -> HTTPDigestCredentials:
    """Split a `key=value, key="quoted value"` Digest field list.

    RFC 7616 Sec. 3.4 - the field set is open-ended, so we collect every
    pair and assign known names to the credential's slots. Unknown
    fields are ignored (e.g. `userhash=true` extensions). Quoted
    values are unwrapped per RFC 5322 quoted-pair semantics; unquoted
    values pass through verbatim.
    """
    _, fields = parse_header_params(value, delimiter=",", unescape=True)
    return HTTPDigestCredentials(
        username=fields.get("username", ""),
        realm=fields.get("realm", ""),
        nonce=fields.get("nonce", ""),
        uri=fields.get("uri", ""),
        response=fields.get("response", ""),
        qop=fields.get("qop", ""),
        nc=fields.get("nc", ""),
        cnonce=fields.get("cnonce", ""),
        opaque=fields.get("opaque", ""),
        algorithm=fields.get("algorithm", ""),
    )


class HTTPBearer(_BearerScheme):
    """HTTP Bearer token authentication."""

    # `auto_error` and `_bearer_scheme` are owned by the base; `scheme_name`
    # is the public attribute mirrored into `_bearer_scheme` for the shared
    # `__call__`.
    __slots__ = ("scheme_name", "_bearer_scheme")

    def __init__(self, auto_error: bool = True, scheme_name: str = AUTH_SCHEME_BEARER) -> None:
        self.auto_error = auto_error
        self.scheme_name = scheme_name
        self._bearer_scheme = scheme_name
