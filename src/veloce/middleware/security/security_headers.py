"""Security-headers middleware — sets the common hardening response headers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from veloce._constants import (
    HEADER_CONTENT_SECURITY_POLICY,
    HEADER_PERMISSIONS_POLICY,
    HEADER_REFERRER_POLICY,
    HEADER_STRICT_TRANSPORT_SECURITY,
    HEADER_VALUE_DENY,
    HEADER_VALUE_NOSNIFF,
    HEADER_VALUE_STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
    HEADER_X_CONTENT_TYPE_OPTIONS,
    HEADER_X_FRAME_OPTIONS,
)
from veloce.audit import Finding
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    from veloce.audit import AuditContext


# ── Hardening headers ─────────────────────────────────────
class SecurityHeadersMiddleware(Middleware):
    """Attach common hardening response headers to every response.

    Set by default:

    - ``X-Content-Type-Options: nosniff`` - stop MIME sniffing.
    - ``X-Frame-Options: DENY`` - block framing (clickjacking).
    - ``Referrer-Policy: strict-origin-when-cross-origin``.

    Off unless configured:

    - ``Strict-Transport-Security`` - pass ``hsts_max_age`` (seconds).
      Browsers honour HSTS only over HTTPS, so it is inert in plain-HTTP
      development, but it is still opt-in because it pins clients to
      HTTPS for the configured lifetime.
    - ``Content-Security-Policy`` - pass ``content_security_policy``.
    - ``Permissions-Policy`` - pass ``permissions_policy``.

    A header a handler already set on the response is left untouched -
    these are defaults, not overrides.
    """

    sets_hardening_headers = True

    def __init__(
        self,
        *,
        content_type_options: str | None = HEADER_VALUE_NOSNIFF,
        frame_options: str | None = HEADER_VALUE_DENY,
        referrer_policy: str | None = HEADER_VALUE_STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
        hsts_max_age: int | None = None,
        hsts_include_subdomains: bool = False,
        hsts_preload: bool = False,
        content_security_policy: str | None = None,
        permissions_policy: str | None = None,
        name: str | None = None,
    ) -> None:
        """Choose the default headers.

        Args:
            content_type_options: `X-Content-Type-Options` value; `None` omits it.
            frame_options: `X-Frame-Options` value; `None` omits it.
            referrer_policy: `Referrer-Policy` value; `None` omits it.
            hsts_max_age: `Strict-Transport-Security` lifetime in seconds; `None` omits it.
            hsts_include_subdomains: Add `includeSubDomains` to HSTS.
            hsts_preload: Add `preload` to HSTS.
            content_security_policy: A fixed `Content-Security-Policy` value.
            permissions_policy: A `Permissions-Policy` value.
            name: Name for `exclude_middleware`.

        """
        super().__init__(name=name)
        # Resolved once so the per-response cost is copying a small fixed dict.
        headers: dict[str, str] = {}
        if content_type_options:
            headers[HEADER_X_CONTENT_TYPE_OPTIONS] = content_type_options
        if frame_options:
            headers[HEADER_X_FRAME_OPTIONS] = frame_options
        if referrer_policy:
            headers[HEADER_REFERRER_POLICY] = referrer_policy
        if hsts_max_age is not None:
            value = f"max-age={hsts_max_age}"
            if hsts_include_subdomains:
                value += "; includeSubDomains"
            if hsts_preload:
                value += "; preload"
            headers[HEADER_STRICT_TRANSPORT_SECURITY] = value
        if content_security_policy:
            headers[HEADER_CONTENT_SECURITY_POLICY] = content_security_policy
        if permissions_policy:
            headers[HEADER_PERMISSIONS_POLICY] = permissions_policy
        self._headers = headers
        # The lowered form is compared against the response's keys per response.
        self._header_items: tuple[tuple[str, str, str], ...] = tuple(
            (name, name.lower(), value) for name, value in headers.items()
        )

    def audit(self, ctx: AuditContext) -> Iterable[Finding]:
        """Note the opt-in headers this instance is not sending.

        `Strict-Transport-Security` and `Content-Security-Policy` stay off
        until asked for, which leaves a registered instance looking hardened
        while sending neither. Informational: an app served over plain HTTP, or
        one whose policy is set at a proxy, is right to leave them off.
        """
        if HEADER_STRICT_TRANSPORT_SECURITY not in self._headers:
            yield Finding(
                "SecurityHeadersMiddleware sends no Strict-Transport-Security, so a browser "
                "can be talked back onto plain HTTP.",
                severity="info",
                fix="pass hsts_max_age=31536000 (one year)",
                id="hsts-not-sent",
            )
        if HEADER_CONTENT_SECURITY_POLICY not in self._headers:
            yield Finding(
                "SecurityHeadersMiddleware sends no Content-Security-Policy, the header that "
                "limits where scripts may load from.",
                severity="info",
                fix="pass content_security_policy=\"default-src 'self'\" and widen from there",
                id="csp-not-sent",
            )

    async def process_response(self, request: Request, response: Response) -> Response:
        """Attach security hardening headers to every response."""
        # Defaults only - never clobber a handler's value, matched case-insensitively
        # so a lowercase `x-frame-options` still wins.
        headers = response.headers
        if not headers:
            headers.update(self._headers)
            return response
        present = {key.lower() for key in headers}
        for name, lowered, value in self._header_items:
            if lowered not in present:
                headers[name] = value
        return response
