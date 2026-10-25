"""HTTP authentication schemes — Basic, Digest, Bearer."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from typing_extensions import Doc

from veloce._constants import HEADER_WWW_AUTHENTICATE, MSG_NOT_AUTHENTICATED
from veloce._header_parsing import parse_header_params
from veloce._internal import _decode_basic_credentials
from veloce._protocol_constants import AUTH_SCHEME_BASIC, AUTH_SCHEME_BEARER, AUTH_SCHEME_DIGEST
from veloce.exceptions import HTTPException
from veloce.http.request import Request
from veloce.security._utils import _AUTHORIZATION_KEY, _quote_header_value, _validate_realm
from veloce.security.base import SecurityScheme, _BearerScheme
from veloce.status import HTTP_401_UNAUTHORIZED

_BASIC_PREFIX = (AUTH_SCHEME_BASIC + " ").lower()
_BASIC_PREFIX_LEN = len(_BASIC_PREFIX)
_DIGEST_PREFIX = (AUTH_SCHEME_DIGEST + " ").lower()
_DIGEST_PREFIX_LEN = len(_DIGEST_PREFIX)


def _default_nonce() -> str:
    """Generate an opaque nonce for the Digest challenge.

    16 random bytes hex-encoded - well above the 64-bit entropy floor RFC 7616
    Sec. 5.3 recommends. Nonce replay tracking is application territory.
    """
    return secrets.token_hex(16)


def _parse_digest(value: str) -> HTTPDigestCredentials:
    """Split a `key=value, key="quoted value"` Digest field list.

    RFC 7616 Sec. 3.4 leaves the field set open-ended, so unknown fields are
    ignored. Quoted values are unwrapped per RFC 5322 quoted-pair semantics.
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


@dataclass(slots=True, repr=False, eq=False)
class HTTPBasicCredentials:
    """HTTP Basic auth credentials.

    `eq=False` keeps identity equality, and so hashability: a generated `__eq__`
    sets `__hash__` to `None` and the credentials stop working as a dict key.
    """

    username: str
    password: str

    def __repr__(self) -> str:
        # Masked: this object sits in the frame locals of everything downstream of
        # `Depends(HTTPBasic())`, so a field-rendering repr would write the
        # plaintext into any error tracker that captures locals.
        return f"HTTPBasicCredentials(username={self.username!r}, password='***')"


class HTTPBasic(SecurityScheme):
    """HTTP Basic authentication - extracts username:password from Authorization header."""

    # `auto_error` is owned by `SecurityScheme`'s slots.
    __slots__ = ("realm", "_challenge_template")

    def __init__(
        self,
        auto_error: Annotated[
            bool,
            Doc("Raise 401 when credentials are absent; False resolves to None."),
        ] = True,
        realm: Annotated[
            str,
            Doc("Realm published in the `WWW-Authenticate` challenge on a 401."),
        ] = "",
    ) -> None:
        _validate_realm(realm)
        self.auto_error = auto_error
        self.realm = realm
        # The challenge is request-invariant, so build it once as a template that
        # `_challenge()` copies: middleware mutating `response.headers` in place
        # would otherwise carry one request's Set-Cookie onto every challenge.
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
        auth = request._peek_header_key(_AUTHORIZATION_KEY) or ""
        if auth[:_BASIC_PREFIX_LEN].lower() != _BASIC_PREFIX:
            if self.auto_error:
                raise HTTPException(
                    HTTP_401_UNAUTHORIZED,
                    MSG_NOT_AUTHENTICATED,
                    headers=self._challenge(),
                )
            return None

        # `None` covers both malformed shapes: base64 that is not UTF-8, and the
        # colon-less value RFC 7617 Sec. 2 forbids as an empty-password login.
        decoded_pair = _decode_basic_credentials(auth[_BASIC_PREFIX_LEN:])
        if decoded_pair is None:
            raise HTTPException(
                HTTP_401_UNAUTHORIZED,
                "Invalid authentication credentials",
                headers=self._challenge(),
            )
        username, password = decoded_pair
        return HTTPBasicCredentials(username=username, password=password)

    def openapi_scheme(self) -> dict[str, Any] | None:
        """HTTP authentication, published with the scheme it advertises."""
        return {"type": "http", "scheme": "basic"}


@dataclass(slots=True, repr=False, eq=False)
class HTTPDigestCredentials:
    """Parsed Digest auth challenge response - RFC 7616 Sec. 3.4.

    `eq=False` keeps identity equality, as on `HTTPBasicCredentials`.
    """

    username: str = ""
    realm: str = ""
    nonce: str = ""
    uri: str = ""
    response: str = ""
    qop: str = ""
    nc: str = ""
    cnonce: str = ""
    opaque: str = ""
    algorithm: str = ""

    def __repr__(self) -> str:
        # `response` is the keyed digest that authenticates the request (RFC 7616
        # Sec. 3.4): credential material, replayable for the nonce's lifetime.
        return (
            f"HTTPDigestCredentials(username={self.username!r}, realm={self.realm!r}, "
            f"nonce={self.nonce!r}, uri={self.uri!r}, response='***', qop={self.qop!r}, "
            f"nc={self.nc!r}, cnonce={self.cnonce!r}, opaque={self.opaque!r}, "
            f"algorithm={self.algorithm!r})"
        )


class HTTPDigest(SecurityScheme):
    """HTTP Digest authentication - RFC 7616.

    Parses the `Authorization: Digest ...` header into `HTTPDigestCredentials`
    and emits a 401 + `WWW-Authenticate: Digest ...` challenge when auth is
    missing or malformed. **This class does NOT validate the response hash** -
    the application owns the secret (HA1) and must compute the expected digest
    itself; Digest's whole point is that the secret never crosses the wire.
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
        realm: Annotated[
            str,
            Doc("Protection space the digest challenge names. Required."),
        ],
        qop: Annotated[
            str,
            Doc("Quality of protection offered - `auth` or `auth-int` (RFC 7616)."),
        ] = "auth",
        algorithm: Annotated[
            str,
            Doc("Digest algorithm published in the challenge."),
        ] = "SHA-256",
        auto_error: Annotated[
            bool,
            Doc("Raise 401 when credentials are absent; False resolves to None."),
        ] = True,
        nonce_factory: Annotated[
            Callable[[], str] | None,
            Doc("Mint the challenge nonce. Defaults to a cryptographic random."),
        ] = None,
    ) -> None:
        # RFC 7616 Sec. 3.2 prefers SHA-256; MD5 remains accepted for back-compat
        # with RFC 2617 clients but should not be the default for new servers.
        _validate_realm(realm)
        self.realm = realm
        self.qop = qop
        self.algorithm = algorithm
        self.auto_error = auto_error
        self.nonce_factory = nonce_factory or _default_nonce
        # Only the `nonce` varies between challenges, so precompute the constant
        # prefix and suffix and splice the per-call nonce in between.
        self._challenge_prefix = (
            f'{AUTH_SCHEME_DIGEST} realm="{_quote_header_value(realm)}", qop="{qop}", nonce="'
        )
        self._challenge_suffix = f'", algorithm={algorithm}'

    def _challenge_headers(self) -> dict[str, str]:
        # RFC 7616 Sec. 3.3 - param names are case-insensitive but the
        # quoted-string values must be exact; clients reject malformed challenges.
        nonce = self.nonce_factory()
        return {HEADER_WWW_AUTHENTICATE: self._challenge_prefix + nonce + self._challenge_suffix}

    def __call__(self, request: Request) -> HTTPDigestCredentials | None:
        auth = request._peek_header_key(_AUTHORIZATION_KEY) or ""
        if auth[:_DIGEST_PREFIX_LEN].lower() != _DIGEST_PREFIX:
            if self.auto_error:
                raise HTTPException(
                    HTTP_401_UNAUTHORIZED,
                    MSG_NOT_AUTHENTICATED,
                    headers=self._challenge_headers(),
                )
            return None
        return _parse_digest(auth[_DIGEST_PREFIX_LEN:])

    def openapi_scheme(self) -> dict[str, Any] | None:
        """HTTP authentication, published with the scheme it advertises."""
        return {"type": "http", "scheme": "digest"}


class HTTPBearer(_BearerScheme):
    """HTTP Bearer token authentication."""

    # `auto_error` and `_bearer_scheme` are owned by the base; `scheme_name` is
    # mirrored into `_bearer_scheme` for the shared `__call__`.
    __slots__ = ("scheme_name", "_bearer_scheme")

    def __init__(
        self,
        auto_error: Annotated[
            bool,
            Doc("Raise 401 when the token is absent; False resolves to None."),
        ] = True,
        scheme_name: Annotated[
            str,
            Doc("Authorization scheme accepted and published, e.g. `Bearer`."),
        ] = AUTH_SCHEME_BEARER,
    ) -> None:
        self.auto_error = auto_error
        self.scheme_name = scheme_name
        self._bearer_scheme = scheme_name

    def openapi_scheme(self) -> dict[str, Any] | None:
        """HTTP authentication, published with the scheme it advertises.

        The scheme is `scheme_name`, which is also what `__call__` matches the
        `Authorization` header against. Lower-cased because OpenAPI 3.1 names the
        IANA registry entry, whose entries are lower-case.
        """
        return {"type": "http", "scheme": self.scheme_name.lower()}
