"""Security scheme base — the shared callable contract for auth schemes.

Every authentication scheme (`HTTPBasic`, `HTTPBearer`, the API-key schemes,
the OAuth2/OIDC schemes) is a callable object the dependency resolver invokes
as `scheme(request)`. `SecurityScheme` captures that one contract plus the
`auto_error` field every scheme carries, so the field and the `__call__`
signature live in one place instead of being re-declared per file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veloce._internal import _require_slots
from veloce.security._utils import _extract_bearer_token

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.request import Request


class SecurityScheme:
    """Base contract for callable authentication schemes.

    Owns the `auto_error` field and the resolver's expected shape: a
    `__call__(self, request)` returning the extracted credential, or `None`
    when authentication is absent and `auto_error` is `False`. Not an
    `abc.ABC`: the hook raises `NotImplementedError` instead.
    """

    # The only field common to every scheme, so subclasses need not declare it.
    __slots__ = ("auto_error",)

    auto_error: bool

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # A subclass omitting `__slots__` would regain a per-instance `__dict__`.
        super().__init_subclass__(**kwargs)
        _require_slots(cls)

    def __call__(self, request: Request) -> Any:
        """Extract the credential from the request, or 401 when `auto_error`."""
        raise NotImplementedError

    def openapi_scheme(self) -> dict[str, Any] | None:
        """Describe this scheme as an OpenAPI Security Scheme Object.

        Return the object published under `components.securitySchemes`, or
        `None` when the scheme cannot be described - a route guarded by an
        undescribed scheme publishes no security requirement, which asserts the
        endpoint is open, so the schema build warns rather than doing it
        silently. A subclass implementing this is published like a built-in.

        Usage::

            class CertHeaderAuth(SecurityScheme):
                __slots__ = ()

                def openapi_scheme(self):
                    return {"type": "apiKey", "in": "header", "name": "X-Cert"}
        """
        return None


class _BearerScheme(SecurityScheme):
    """Shared `Authorization: Bearer` extraction.

    Subclasses set `_bearer_scheme` (the advertised auth-scheme token) and
    inherit this `__call__`.
    """

    __slots__ = ()

    # The auth-scheme token clients send; `Bearer` unless a subclass overrides.
    _bearer_scheme: str

    def __call__(self, request: Request) -> str | None:
        return _extract_bearer_token(request, self._bearer_scheme, self.auto_error)
