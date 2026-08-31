"""OAuth2 — authentication schemes.

Some constructor parameters use camelCase (`authorizationUrl`, `tokenUrl`,
`refreshUrl`, `openIdConnectUrl`) rather than the project's snake_case
convention. These names mirror the OAuth2/OpenID Connect security-scheme
field names defined by the OpenAPI specification, so a scheme description
copied from a standard OpenAPI document maps onto these classes without
renaming. They are kept as-is deliberately for spec compliance.
"""

from __future__ import annotations

from typing import Annotated, Any, TypeVar

from typing_extensions import Doc

from veloce._params import Form
from veloce._protocol_constants import AUTH_SCHEME_BEARER, OAUTH2_GRANT_TYPE_PASSWORD
from veloce.exceptions import HTTPException
from veloce.http.request import Request
from veloce.security.base import _BearerScheme
from veloce.status import HTTP_422_UNPROCESSABLE_ENTITY


def _form_value(v: Any) -> Any:
    """Resolve a constructor argument that may still be a `Form` marker.

    Under the dependency resolver each field arrives as the resolved request
    value; when the form is constructed directly the arguments are still the
    `Form()` markers, so fall back to their declared defaults.
    """
    return v.default if isinstance(v, Form) else v


class _OAuth2BearerScheme(_BearerScheme):
    """Shared Bearer-token extraction for the OAuth2/OIDC schemes.

    `OAuth2PasswordBearer`, `OAuth2AuthorizationCodeBearer`, and
    `OpenIdConnect` pull the same `Authorization: Bearer` token; they
    differ only in the OpenAPI scheme they advertise. Each keeps its own
    `__init__` (and distinct fields) and inherits the shared `__call__` from
    `_BearerScheme` so the extraction lines stay in one place.
    """

    __slots__ = ()
    _bearer_scheme = AUTH_SCHEME_BEARER


class OAuth2PasswordBearer(_OAuth2BearerScheme):
    """OAuth2 Password Bearer flow - extracts token from Authorization header."""

    __slots__ = ("token_url", "scopes")

    def __init__(
        self,
        token_url: Annotated[
            str,
            Doc("Endpoint the password flow exchanges credentials at."),
        ],
        auto_error: Annotated[
            bool,
            Doc("Raise 401 when the token is absent; False resolves to None."),
        ] = True,
        scopes: Annotated[
            dict[str, str] | None,
            Doc("Scope name to description, published in the OpenAPI document."),
        ] = None,
    ) -> None:
        self.token_url = token_url
        self.auto_error = auto_error
        self.scopes = scopes or {}

    def openapi_scheme(self) -> dict[str, Any] | None:
        """Describe the password flow, with its token endpoint and scopes."""
        return {
            "type": "oauth2",
            "flows": {
                OAUTH2_GRANT_TYPE_PASSWORD: {
                    "tokenUrl": self.token_url,
                    "scopes": self.scopes,
                }
            },
        }


class OAuth2AuthorizationCodeBearer(_OAuth2BearerScheme):
    """OAuth2 Authorization-Code (with PKCE) Bearer flow.

    Extracts a Bearer token from the `Authorization:` header exactly
    like `OAuth2PasswordBearer`; the difference is the OpenAPI security
    scheme it advertises (`authorizationUrl` + `tokenUrl` + scopes),
    which is what an interactive OAuth2 client (Swagger UI's "Authorize"
    button, an SPA's auth library) uses to start the redirect dance.

    The construction shape is chosen so an OpenAPI snippet generated from a
    standard OpenAPI document can be replayed against veloce without rewrites:

        oauth2 = OAuth2AuthorizationCodeBearer(
            authorizationUrl="https://auth.example.com/authorize",
            tokenUrl="https://auth.example.com/token",
            refreshUrl=None,
            scopes={"read:items": "Read items", "write:items": "Write items"},
            auto_error=True,
        )
    """

    __slots__ = ("authorizationUrl", "tokenUrl", "refreshUrl", "scopes")

    def __init__(
        self,
        authorizationUrl: Annotated[
            str,
            Doc("Endpoint the user agent is sent to for authorization."),
        ],
        tokenUrl: Annotated[
            str,
            Doc("Endpoint the authorization code is exchanged for a token at."),
        ],
        refreshUrl: Annotated[
            str | None,
            Doc("Endpoint refresh tokens are redeemed at. Omitted when unset."),
        ] = None,
        scopes: Annotated[
            dict[str, str] | None,
            Doc("Scope name to description, published in the OpenAPI document."),
        ] = None,
        auto_error: Annotated[
            bool,
            Doc("Raise 401 when the token is absent; False resolves to None."),
        ] = True,
    ) -> None:
        self.authorizationUrl = authorizationUrl
        self.tokenUrl = tokenUrl
        self.refreshUrl = refreshUrl
        self.scopes = scopes or {}
        self.auto_error = auto_error

    def openapi_scheme(self) -> dict[str, Any] | None:
        """Describe the authorization-code flow; `refreshUrl` is omitted when unset."""
        flow: dict[str, Any] = {
            "authorizationUrl": self.authorizationUrl,
            "tokenUrl": self.tokenUrl,
            "scopes": self.scopes,
        }
        if self.refreshUrl:
            flow["refreshUrl"] = self.refreshUrl
        return {"type": "oauth2", "flows": {"authorizationCode": flow}}


class OpenIdConnect(_OAuth2BearerScheme):
    """OpenID Connect Bearer authentication.

    Same Bearer extraction logic as the OAuth2 schemes; the OpenAPI
    scheme advertises a single `openIdConnectUrl` pointing at the
    provider's `.well-known/openid-configuration` document. Clients
    auto-discover everything else from there.
    """

    __slots__ = ("openIdConnectUrl",)

    def __init__(
        self,
        openIdConnectUrl: Annotated[
            str,
            Doc("OpenID Connect discovery document URL."),
        ],
        auto_error: Annotated[
            bool,
            Doc("Raise 401 when the token is absent; False resolves to None."),
        ] = True,
    ) -> None:
        self.openIdConnectUrl = openIdConnectUrl
        self.auto_error = auto_error

    def openapi_scheme(self) -> dict[str, Any] | None:
        """OpenID Connect discovery, published as its single URL."""
        return {"type": "openIdConnect", "openIdConnectUrl": self.openIdConnectUrl}


#: Bound to the form class so `_from_form` returns the *subclass* it was called
#: on. `typing.Self` would say this directly but is 3.11+, and this package
#: supports 3.10 - which is why the strict subclass carried a
#: `# type: ignore[return-value]` instead of a type.
_FormT = TypeVar("_FormT", bound="OAuth2PasswordRequestForm")


class OAuth2PasswordRequestForm:
    """OAuth2 password request form data."""

    __slots__ = ("username", "password", "scope", "client_id", "client_secret", "grant_type")

    def __init__(
        self,
        # Annotated `Any`: the default is a `Form` marker, but the
        # resolver passes a real `str` value at call time.
        grant_type: Any = Form(default=OAUTH2_GRANT_TYPE_PASSWORD),
        username: Any = Form(default=""),
        password: Any = Form(default=""),
        scope: Any = Form(default=""),
        client_id: Any = Form(default=None),
        client_secret: Any = Form(default=None),
    ) -> None:
        # Used as a `Depends()` class dependency: the resolver reads each
        # field from the request form body via the `Form()` markers and
        # passes resolved values here. When constructed directly the
        # params are still `Form` markers - fall back to their defaults.
        self.grant_type = _form_value(grant_type)
        self.username = _form_value(username)
        self.password = _form_value(password)
        self.scope = _form_value(scope)
        self.client_id = _form_value(client_id)
        self.client_secret = _form_value(client_secret)

    @classmethod
    async def from_request(cls, request: Request) -> OAuth2PasswordRequestForm:
        """Parse an OAuth2 password grant from the request form data."""
        form_data = await request.form()
        return cls._from_form(form_data)

    @classmethod
    def _from_form(cls: type[_FormT], form_data: Any) -> _FormT:
        return cls(
            username=form_data.get("username", ""),
            password=form_data.get("password", ""),
            scope=form_data.get("scope", ""),
            client_id=form_data.get("client_id"),
            client_secret=form_data.get("client_secret"),
            grant_type=form_data.get("grant_type", OAUTH2_GRANT_TYPE_PASSWORD),
        )


class OAuth2PasswordRequestFormStrict(OAuth2PasswordRequestForm):
    """`OAuth2PasswordRequestForm` with a mandatory `grant_type`.

    The non-strict form leaves `grant_type` optional;
    the strict form *requires* it and constrains the value to the
    literal `password` (RFC 6749 Sec. 4.3.2). Missing or mismatched values
    fail validation with 422.
    """

    __slots__ = ()

    def __init__(
        self,
        grant_type: Any = Form(regex=f"^{OAUTH2_GRANT_TYPE_PASSWORD}$"),
        username: Any = Form(default=""),
        password: Any = Form(default=""),
        scope: Any = Form(default=""),
        client_id: Any = Form(default=None),
        client_secret: Any = Form(default=None),
    ) -> None:
        super().__init__(
            grant_type=grant_type,
            username=username,
            password=password,
            scope=scope,
            client_id=client_id,
            client_secret=client_secret,
        )

    @classmethod
    async def from_request(cls, request: Request) -> OAuth2PasswordRequestFormStrict:
        """Parse and validate that grant_type is present and equals 'password'."""
        form_data = await request.form()
        grant_type = form_data.get("grant_type")
        # Strict: missing OR not exactly "password" both fail; the non-strict
        # parent defaults a missing value to "password", which masks absence.
        if grant_type is None or grant_type != OAUTH2_GRANT_TYPE_PASSWORD:
            raise HTTPException(
                HTTP_422_UNPROCESSABLE_ENTITY,
                f"grant_type must be {OAUTH2_GRANT_TYPE_PASSWORD!r}",
            )
        return cls._from_form(form_data)
