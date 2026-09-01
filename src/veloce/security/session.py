"""Session authentication — turn a signed-in session into a `Principal`.

`SessionAuth` is the cookie-session counterpart to the bearer and API-key
schemes: it reads the identity a login handler stored on `request.session` and
publishes it through `set_principal`, so a permission check written against
`current_principal()` behaves the same whether the caller arrived over HTTP
with a session cookie or over MCP with a token.

Without it, `request.session` and `Principal` are two unrelated notions of
"who is calling": a session-logged-in user resolves to `current_principal()
is None`, and any guard written against the principal sees an anonymous
caller.

Session cookie security attributes are RFC 6265 section 4.1.2; the id rotation
performed at login is the session-fixation defence (OWASP Session Management).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from veloce._constants import MSG_NOT_AUTHENTICATED
from veloce.exceptions import Unauthorized
from veloce.principal import Principal, set_principal
from veloce.security.base import SecurityScheme

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.request import Request

# Default session key holding the authenticated subject. It is the default on
# both sides of the contract - `SessionAuth(subject_key=...)` reads it and
# `login_session(subject_key=...)` writes it - so an application that overrides
# one has to pass the same key to the other.
SESSION_SUBJECT_KEY = "_auth_subject"

# Default session key holding the granted scopes, stored as a list because a
# session payload round-trips through JSON. Overridable on both sides like
# `SESSION_SUBJECT_KEY`.
SESSION_SCOPES_KEY = "_auth_scopes"

# The cookie `SessionMiddleware` writes by default. `SessionAuth` cannot read the
# middleware's configuration - it is constructed independently of it - so this is
# what it advertises in the schema unless told otherwise.
DEFAULT_SESSION_COOKIE_NAME = "session"


class SessionAuth(SecurityScheme):
    """Resolve the current `Principal` from the request's session.

    Usage::

        from veloce import Depends, Veloce
        from veloce.security.session import SessionAuth, login_session

        app = Veloce(secret_key="...")
        app.add_middleware(SessionMiddleware, secret_key="...")
        session_auth = SessionAuth()

        @app.post("/login")
        async def login(request: Request):
            login_session(request, "user-42", scopes={"items:read"})
            return {"ok": True}

        @app.get("/me")
        async def me(principal=Depends(session_auth)):
            return {"user": principal.subject}

    Returns the `Principal` and publishes it via `set_principal`, so
    `current_principal()` resolves for anything further down the request -
    including a dependency shared with an MCP-exposed handler.

    With `auto_error=False` an anonymous request resolves to `None` instead of
    raising, for routes that render differently when signed in. A missing
    `SessionMiddleware` is a configuration error rather than an anonymous
    request, and still raises under either setting.

    Pass `loader=` to build a richer principal from the stored subject (a
    database lookup, say); it receives `(request, subject)` and returns a
    `Principal`, or `None` to reject the session.

    The OpenAPI document describes this as an `apiKey` credential read from the
    session cookie. Pass `cookie_name=` when `SessionMiddleware` is configured
    with a name other than the default, so the document names the cookie a
    client actually has to send.

    `subject_key=` / `scopes_key=` name the session slots this reads. They are
    the same slots `login_session` writes, so an application that overrides
    either must pass the matching key to `login_session` too - otherwise the
    scheme reads a slot the login never wrote and every request is anonymous.
    """

    __slots__ = ("cookie_name", "loader", "scopes_key", "subject_key")

    def __init__(
        self,
        *,
        auto_error: Annotated[
            bool,
            Doc("Raise 401 when the session carries no subject; False resolves to None."),
        ] = True,
        subject_key: Annotated[
            str,
            Doc("Session key holding the authenticated subject."),
        ] = SESSION_SUBJECT_KEY,
        scopes_key: Annotated[
            str,
            Doc("Session key holding the granted scopes."),
        ] = SESSION_SCOPES_KEY,
        loader: Annotated[
            Callable[[Request, str], Principal | None] | None,
            Doc("Build the principal from the stored subject instead of the default mapping."),
        ] = None,
        cookie_name: Annotated[
            str,
            Doc("Session cookie name to publish in the OpenAPI document."),
        ] = DEFAULT_SESSION_COOKIE_NAME,
    ) -> None:
        self.auto_error = auto_error
        self.subject_key = subject_key
        self.scopes_key = scopes_key
        self.loader = loader
        self.cookie_name = cookie_name

    def __call__(self, request: Request) -> Principal | None:
        """Return the session's `Principal`, publishing it for the request."""
        # `Request.session` raises an actionable RuntimeError when
        # SessionMiddleware is missing, and that is left to surface: a scheme
        # that treated the absence as "anonymous" would report every caller as
        # signed out forever, which is a silent authentication failure.
        # `auto_error=False` covers an anonymous *request*, not an unconfigured
        # application.
        session: Any = request.session
        subject = session.get(self.subject_key)
        if not subject:
            if self.auto_error:
                raise Unauthorized(MSG_NOT_AUTHENTICATED)
            return None

        if self.loader is not None:
            principal = self.loader(request, subject)
            if principal is None:
                if self.auto_error:
                    raise Unauthorized(MSG_NOT_AUTHENTICATED)
                return None
        else:
            principal = Principal(
                subject=subject,
                scopes=frozenset(session.get(self.scopes_key) or ()),
            )
        set_principal(principal)
        return principal

    def openapi_scheme(self) -> dict[str, Any] | None:
        """Describe the session credential, as a cookie-borne API key.

        OpenAPI has no session-specific scheme type; a cookie credential is an
        `apiKey` read from `cookie`, which is how `APIKeyCookie` describes the
        same transport.
        """
        return {"type": "apiKey", "in": "cookie", "name": self.cookie_name}


def login_session(
    request: Request,
    subject: str,
    *,
    scopes: Iterable[str] = (),
    subject_key: str = SESSION_SUBJECT_KEY,
    scopes_key: str = SESSION_SCOPES_KEY,
    **claims: Any,
) -> None:
    """Sign `subject` into the request's session and publish the principal.

    Rotates the session id first, so a session id planted before login cannot
    be replayed against the now-authenticated session.

    `subject_key` / `scopes_key` must name the same slots as the `SessionAuth`
    that reads the session back. A scheme built with a non-default key reads a
    slot this helper never wrote, and every request resolves anonymous.
    """
    session = request.session
    session.regenerate_id()
    session[subject_key] = subject
    scope_list = list(scopes)
    if scope_list:
        session[scopes_key] = scope_list
    else:
        session.pop(scopes_key, None)
    for key, value in claims.items():
        session[key] = value
    set_principal(Principal(subject=subject, scopes=frozenset(scope_list), claims=dict(claims)))


def logout_session(request: Request) -> None:
    """Clear the session's identity and the request's principal.

    Clears the whole session rather than only the identity keys: leftover
    per-user state on a session that has changed hands is a data-leak shape,
    not a convenience.
    """
    session = request.session
    session.clear()
    session.regenerate_id()
    set_principal(None)
