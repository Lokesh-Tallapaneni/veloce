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

    Owns the `auto_error` field and documents the resolver's expected shape:
    a `__call__(self, request)` that returns the extracted credential, or
    `None` when authentication is absent and `auto_error` is `False`. Not an
    `abc.ABC`: the hook raises `NotImplementedError` so subclasses that forget
    to override fail loudly without pulling in the ABC machinery.
    """

    # `auto_error` is the only field common to every scheme; owning the slot
    # here keeps subclasses' own `__slots__` free of it.
    __slots__ = ("auto_error",)

    auto_error: bool

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # Mirror the `__slots__` guard the security schemes already rely on:
        # a subclass that omits `__slots__` would regain a per-instance
        # `__dict__`, defeating the slotted layout the base establishes.
        super().__init_subclass__(**kwargs)
        _require_slots(cls)

    def __call__(self, request: Request) -> Any:
        """Extract the credential from the request, or 401 when `auto_error`."""
        raise NotImplementedError

    def openapi_scheme(self) -> dict[str, Any] | None:
        """Describe this scheme as an OpenAPI Security Scheme Object.

        Return the object published under `components.securitySchemes`, or
        `None` when the scheme cannot be described. A route guarded by an
        undescribed scheme is published with no security requirement - which
        asserts the endpoint is open - so the schema build warns rather than
        doing that silently.

        Only the scheme knows what it reads and how a client should send it,
        so it answers for itself: a subclass adding a new authentication style
        implements this and is published like a built-in.

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
    inherit this `__call__`, so the one-line delegation to
    `_extract_bearer_token` is written exactly once.
    """

    __slots__ = ()

    # The auth-scheme token clients send (`Bearer` by default); subclasses may
    # override to advertise a different scheme name.
    _bearer_scheme: str

    def __call__(self, request: Request) -> str | None:
        return _extract_bearer_token(request, self._bearer_scheme, self.auto_error)
