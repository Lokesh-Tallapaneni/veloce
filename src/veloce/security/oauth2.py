"""OAuth2 authentication schemes."""

from __future__ import annotations

from typing import Any

from veloce.exceptions import HTTPException
from veloce.http.request import Request
from veloce.routing.params import Form


class OAuth2PasswordBearer:
    """OAuth2 Password Bearer flow — extracts token from Authorization header."""

    def __init__(
        self,
        token_url: str,
        auto_error: bool = True,
        scopes: dict[str, str] | None = None,
    ) -> None:
        self.token_url = token_url
        self.auto_error = auto_error
        self.scopes = scopes or {}

    def __call__(self, request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            if self.auto_error:
                raise HTTPException(
                    401,
                    "Not authenticated",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return None
        return auth[7:]


class OAuth2AuthorizationCodeBearer:
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

    def __init__(
        self,
        authorizationUrl: str,
        tokenUrl: str,
        refreshUrl: str | None = None,
        scopes: dict[str, str] | None = None,
        auto_error: bool = True,
    ) -> None:
        self.authorizationUrl = authorizationUrl
        self.tokenUrl = tokenUrl
        self.refreshUrl = refreshUrl
        self.scopes = scopes or {}
        self.auto_error = auto_error

    def __call__(self, request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            if self.auto_error:
                raise HTTPException(
                    401,
                    "Not authenticated",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return None
        return auth[7:]


class OpenIdConnect:
    """OpenID Connect Bearer authentication.

    Same Bearer extraction logic as the OAuth2 schemes; the OpenAPI
    scheme advertises a single `openIdConnectUrl` pointing at the
    provider's `.well-known/openid-configuration` document. Clients
    auto-discover everything else from there.
    """

    def __init__(
        self,
        openIdConnectUrl: str,
        auto_error: bool = True,
    ) -> None:
        self.openIdConnectUrl = openIdConnectUrl
        self.auto_error = auto_error

    def __call__(self, request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            if self.auto_error:
                raise HTTPException(
                    401,
                    "Not authenticated",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return None
        return auth[7:]


class OAuth2PasswordRequestForm:
    """OAuth2 password request form data."""

    __slots__ = ("username", "password", "scope", "client_id", "client_secret", "grant_type")

    def __init__(
        self,
        # Annotated `Any`: the default is a `Form` marker, but the
        # resolver passes a real `str` value at call time.
        grant_type: Any = Form(default="password"),
        username: Any = Form(default=""),
        password: Any = Form(default=""),
        scope: Any = Form(default=""),
        client_id: Any = Form(default=None),
        client_secret: Any = Form(default=None),
    ) -> None:
        # Used as a `Depends()` class dependency: the resolver reads each
        # field from the request form body via the `Form()` markers and
        # passes resolved values here. When constructed directly the
        # params are still `Form` markers — fall back to their defaults.
        def _value(v: Any) -> Any:
            return v.default if isinstance(v, Form) else v

        self.grant_type = _value(grant_type)
        self.username = _value(username)
        self.password = _value(password)
        self.scope = _value(scope)
        self.client_id = _value(client_id)
        self.client_secret = _value(client_secret)

    @classmethod
    async def from_request(cls, request: Request) -> OAuth2PasswordRequestForm:
        form_data = await request.form()
        return cls(
            username=form_data.get("username", ""),
            password=form_data.get("password", ""),
            scope=form_data.get("scope", ""),
            client_id=form_data.get("client_id"),
            client_secret=form_data.get("client_secret"),
            grant_type=form_data.get("grant_type", "password"),
        )


class OAuth2PasswordRequestFormStrict(OAuth2PasswordRequestForm):
    """`OAuth2PasswordRequestForm` with a mandatory `grant_type`.

    the non-strict form leaves `grant_type` optional;
    the strict form *requires* it and constrains the value to the
    literal `password` (RFC 6749 §4.3.2). Missing or mismatched values
    fail validation with 422.
    """

    __slots__ = ()

    def __init__(
        self,
        grant_type: Any = Form(regex="^password$"),
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
