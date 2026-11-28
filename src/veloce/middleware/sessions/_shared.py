"""Session middleware helpers — cookie validation, chunking and shared settings."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import Any, Final, Literal

from veloce._constants import HEADER_COOKIE
from veloce.http.response import Response
from veloce.signing import Signer


class _UnsetType:
    """Marks a constructor argument the caller left out."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


# `None` cannot be the marker: it is a meaningful `samesite` value. Narrow
# with `isinstance`, which mypy does not do for a plain singleton.
_UNSET: Final = _UnsetType()

# RFC 6265 Sec. 6.1 mandates 4096 bytes, but some implementations reserve 3.
_DEFAULT_MAX_COOKIE_SIZE = 4093

# Caps the Set-Cookie lines one chunked session can emit, so a huge session is
# dropped rather than exploded (RFC 6265 Sec. 6.1 caps per-domain cookies too).
_DEFAULT_MAX_COOKIE_CHUNKS = 8
# A literal `.` is a valid RFC 6265 token character.
_CHUNK_SEP = "."


def _validate_cookie_security(
    *,
    cookie_prefix: Literal["host", "secure"] | None,
    partitioned: bool,
    domain: str | None,
    path: str,
    secure: bool,
    samesite: str | None,
) -> None:
    """Fail-fast validation of the cookie security config at construction time.

    Enforces the RFC 6265bis name-prefix invariants and the CHIPS
    (`Partitioned`) preconditions at app wiring, where a misconfiguration
    surfaces instead of being silently dropped at response time.
    """
    if cookie_prefix is not None:
        if cookie_prefix not in ("host", "secure"):
            raise ValueError("cookie_prefix must be 'host', 'secure', or None")
        if not secure:
            raise ValueError(f"cookie_prefix={cookie_prefix!r} requires secure=True")
        if cookie_prefix == "host":
            if path != "/":
                raise ValueError("cookie_prefix='host' requires path='/'")
            if domain is not None:
                raise ValueError("cookie_prefix='host' requires domain=None")
    if partitioned and (not secure or (samesite or "").lower() != "none"):
        raise ValueError("partitioned=True (CHIPS) requires secure=True and samesite='none'")
    # Browsers drop a non-Secure SameSite=None cookie (RFC 6265bis same-site).
    if domain is not None and not secure and (samesite or "").lower() == "none":
        warnings.warn(
            "A SameSite=None cookie without Secure is rejected by modern "
            "browsers; a cross-subdomain Domain cookie set this way will be "
            "dropped. Set secure=True.",
            UserWarning,
            stacklevel=3,
        )


def _reassemble_chunks(cookies: Any, base_name: str, max_chunks: int) -> str | None:
    """Rebuild a chunked cookie value from `<base_name>.0`, `.1`, ... cookies.

    The chunks must be contiguous from index 0, and at most `max_chunks` indices
    are read so a forged cookie header cannot drive an unbounded scan. The signed
    value is one URL-safe token, so concatenation inverts the response-side split.
    """
    first = cookies.get(f"{base_name}{_CHUNK_SEP}0")
    if first is None:
        return None
    parts = [first]
    for index in range(1, max_chunks):
        part = cookies.get(f"{base_name}{_CHUNK_SEP}{index}")
        if part is None:
            break
        parts.append(part)
    return "".join(parts)


def _build_signer(secret_key: str | list[str]) -> Signer:
    """Build the session signer; a list rotates secrets (the first one signs)."""
    keys = [secret_key] if isinstance(secret_key, str) else list(secret_key)
    if not keys:
        raise ValueError("secret_key must be a non-empty string or list of strings")
    signer = Signer(keys[0], salt="veloce.session")
    for fallback in keys[1:]:
        signer.add_fallback_secret(fallback, salt="veloce.session")
    return signer


def _wire_name(cookie_prefix: Literal["host", "secure"] | None, cookie_name: str) -> str:
    """Apply the RFC 6265bis name prefix to the configured cookie name.

    The prefix must be on both the request read and the response write, so it is
    derived once at construction and the per-request read stays off the hot path.
    """
    if cookie_prefix == "host":
        return f"__Host-{cookie_name}"
    if cookie_prefix == "secure":
        return f"__Secure-{cookie_name}"
    return cookie_name


def _should_persist(policy: Callable[[int], bool] | None, status_code: int) -> bool:
    """Whether a modified session should be written for this response status.

    The `None` policy default means "persist for status < 500": a failed request
    should not write a half-mutated session.
    """
    return policy(status_code) if policy is not None else status_code < 500


def _begin_session_response(
    vary_on_cookie: bool, session: Any, response: Response
) -> tuple[bool, bool] | None:
    """Shared response-phase preamble for both session middlewares.

    Returns `None` when there is no session to persist, else `(accessed,
    modified)` after emitting `Vary: Cookie` when the handler touched the
    session - without which a URL-keyed cache could share one user's
    session-personalized body with another (RFC 9110 Sec. 12.5.5).
    """
    if session is None:
        return None
    accessed = getattr(session, "accessed", False)
    modified = getattr(session, "modified", False)
    if vary_on_cookie and (accessed or modified):
        response.add_vary(HEADER_COOKIE)
    return accessed, modified


# In one place, so a new shared setting is wired once not per `__init__`.
_SHARED_COOKIE_DEFAULTS: dict[str, Any] = {
    "cookie_name": "session",
    "path": "/",
    "httponly": True,
    "secure": False,
    "samesite": "lax",
}


def _shared_cookie_settings(**supplied: Any) -> dict[str, Any]:
    """Settle the shared cookie settings from the constructor and the defaults.

    The constructor is the only source: a setting the caller did not pass takes
    the library default here and does not change again, so two middlewares can
    carry different cookies.
    """
    return {
        name: _SHARED_COOKIE_DEFAULTS[name] if isinstance(value, _UnsetType) else value
        for name, value in supplied.items()
    }


# Kept only to recognise a configuration that no longer configures anything.
_RETIRED_CONFIG_KEYS: dict[str, str] = {
    "APPLICATION_ROOT": "path",
    "MAX_COOKIE_SIZE": "max_cookie_size",
    "PERMANENT_SESSION_LIFETIME": "permanent_lifetime",
    "SESSION_COOKIE_HTTPONLY": "httponly",
    "SESSION_COOKIE_NAME": "cookie_name",
    "SESSION_COOKIE_SAMESITE": "samesite",
    "SESSION_COOKIE_SECURE": "secure",
}
