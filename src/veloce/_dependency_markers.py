"""Dependency markers — `Depends`, `Security` and `SecurityScopes`."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from veloce._internal import (
    _refuse_string_scopes,
)


class Depends:
    """Dependency marker — use in function signature defaults.

    `dependency` may be omitted (`Depends()`); the resolver then infers
    it from the parameter's type annotation — the shorthand for
    `x: SomeClass = Depends()`.

    Usage::

        def get_db() -> Database:
            return Database()

        @app.get("/users")
        async def list_users(db: Database = Depends(get_db)) -> list[str]:
            return db.all_usernames()
    """

    __slots__ = ("dependency", "use_cache", "offload")

    if TYPE_CHECKING:  # pragma: no cover
        # A marker is written as a parameter's DEFAULT, which a checker refuses
        # as unassignable; the resolver replaces it before the body runs.
        def __new__(cls, *args: Any, **kwargs: Any) -> Any: ...

    def __init__(
        self,
        dependency: Annotated[
            Callable[..., Any] | None,
            Doc(
                "Callable to resolve and inject; inferred from the parameter's type annotation when omitted."
            ),
        ] = None,
        *,
        use_cache: Annotated[
            bool,
            Doc(
                "Cache the resolved value so a dependency referenced more than once per request runs once."
            ),
        ] = True,
        offload: Annotated[
            bool,
            Doc(
                "Run a blocking sync dependency in the thread pool so it cannot stall the event loop."
            ),
        ] = False,
    ) -> None:
        self.dependency = dependency
        self.use_cache = use_cache
        # Defaults off so a trivial pure dependency keeps its inline call.
        self.offload = offload


class Security(Depends):
    """Dependency marker with OAuth2 scopes for OpenAPI emission."""

    __slots__ = ("scopes",)

    def __init__(
        self,
        dependency: Annotated[
            Callable[..., Any] | None,
            Doc(
                "Callable to resolve and inject; inferred from the parameter's type annotation when omitted."
            ),
        ] = None,
        scopes: Annotated[
            list[str] | None,
            Doc(
                "OAuth 2.0 scopes this dependency requires, accumulated for the `SecurityScopes` chain and OpenAPI."
            ),
        ] = None,
        *,
        use_cache: Annotated[
            bool,
            Doc(
                "Cache the resolved value so a dependency referenced more than once per request runs once."
            ),
        ] = True,
        offload: Annotated[
            bool,
            Doc(
                "Run a blocking sync dependency in the thread pool so it cannot stall the event loop."
            ),
        ] = False,
    ) -> None:
        # A `str` is iterable, so `scopes="read"` would grant on `"r"`.
        if isinstance(scopes, (str, bytes)):
            _refuse_string_scopes(scopes, "Security(scopes=...)")
        super().__init__(dependency=dependency, use_cache=use_cache, offload=offload)
        self.scopes = scopes or []


class SecurityScopes:
    """Aggregated OAuth 2.0 scopes for the current Security() chain.

    A handler or sub-dependency declaring a parameter of this type receives the
    union of every `Security(..., scopes=[...])` between the route entry and
    this point in the graph. An authorising dependency checks
    `security_scopes.scopes` against what the bearer token carries and builds a
    `WWW-Authenticate: Bearer scope="<...>"` header when denying.

    Per RFC 6749 Sec. 3.3 the scope-string serialisation is space-separated.
    """

    __slots__ = ("scopes", "scope_str")

    def __init__(self, scopes: list[str] | None = None) -> None:
        self.scopes: list[str] = list(scopes) if scopes else []
        self.scope_str: str = " ".join(self.scopes)

    def __repr__(self) -> str:
        return f"SecurityScopes({self.scopes!r})"
