"""Content Security Policy middleware — policy emission and the per-request nonce."""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence

from veloce._constants import (
    HEADER_CONTENT_SECURITY_POLICY,
    HEADER_CONTENT_SECURITY_POLICY_REPORT_ONLY,
)
from veloce._internal import _current_request_var
from veloce.http.request import Request
from veloce.http.response import Response, header_present
from veloce.middleware.base import Middleware

# request.state slot holding the per-request CSP nonce (materialized lazily).
_CSP_NONCE_STATE_KEY = "csp_nonce"


# ── Content Security Policy ───────────────────────────────
def csp_nonce(request: Request | None = None) -> str | None:
    """Return the per-request CSP nonce, materializing it on first access.

    Pass the request explicitly, or omit it to read the one currently being
    handled. Returns None when `CSPMiddleware` armed no nonce for this request,
    or when there is no request in scope.
    """
    if request is None:
        request = _current_request_var.get()
        if request is None:
            return None
    cached = request._state.get(_CSP_NONCE_STATE_KEY)
    if cached is None:
        return None
    if isinstance(cached, str):
        return cached
    value = cached()
    request._state[_CSP_NONCE_STATE_KEY] = value
    return value


def _normalize_csp(policy: str | Mapping[str, str | Sequence[str]]) -> str:
    """Normalize a CSP policy (str template or directive dict) to a template.

    A dict maps `directive -> source(s)`; the `'nonce'` sentinel source is
    replaced with the runtime `{nonce}` placeholder.
    """
    if isinstance(policy, str):
        return policy
    if isinstance(policy, Mapping):
        segments: list[str] = []
        for directive, sources in policy.items():
            src_list = [sources] if isinstance(sources, str) else list(sources)
            rendered = [("{nonce}" if s == "'nonce'" else s) for s in src_list]
            segments.append(" ".join([directive, *rendered]))
        return "; ".join(segments)
    raise TypeError("CSP policy must be a str template or a directive mapping")


class CSPMiddleware(Middleware):
    """Emit Content-Security-Policy with optional per-request nonce.

    `policy` and `report_only_policy` each accept a str template containing
    the literal `{nonce}` placeholder, or a directive mapping where the
    `'nonce'` source is substituted with a fresh per-request nonce.

    Usage::

        app.add_middleware(
            CSPMiddleware(
                policy={"default-src": "'self'", "script-src": ["'self'", "'nonce'"]},
                report_only_policy="default-src 'self'",
            )
        )

    Static (no-nonce) policies can stay on SecurityHeadersMiddleware; use
    this when a per-request nonce or a report-only policy is needed.
    """

    def __init__(
        self,
        policy: str | Mapping[str, str | Sequence[str]] | None = None,
        *,
        report_only_policy: str | Mapping[str, str | Sequence[str]] | None = None,
        nonce: bool = True,
        name: str | None = None,
    ) -> None:
        """Configure the policy headers.

        Args:
            policy: The enforced policy, as a template or a directive mapping.
            report_only_policy: A policy sent as `Content-Security-Policy-Report-Only`.
            nonce: Mint a per-request nonce for `{nonce}` placeholders.
            name: Name for `exclude_middleware`.

        Raises:
            ValueError: If neither policy is given, or a policy references a nonce
                while `nonce=False`.
            TypeError: If a policy is neither a string nor a mapping.

        """  # noqa: DOC503 - the exception is raised or caught by a callee
        super().__init__(name=name)
        # `ValueError`, not `assert`: `python -O` strips asserts, and an empty
        # configuration would then emit no header while the stack claims it does.
        if policy is None and report_only_policy is None:
            raise ValueError("CSPMiddleware requires at least one of policy or report_only_policy")
        self._enforce_template = _normalize_csp(policy) if policy is not None else None
        self._report_template = (
            _normalize_csp(report_only_policy) if report_only_policy is not None else None
        )
        references_nonce = (
            self._enforce_template is not None and "{nonce}" in self._enforce_template
        ) or (self._report_template is not None and "{nonce}" in self._report_template)
        # A nonce template with generation disabled renders `'nonce-None'`, which
        # a browser parses as a real nonce, silently breaking the policy.
        if not nonce and references_nonce:
            raise ValueError(
                "CSPMiddleware was constructed with nonce=False but a policy "
                "references a nonce ('{nonce}' placeholder or 'nonce' source); "
                "enable nonce=True or remove the nonce reference"
            )
        self._needs_nonce = nonce and references_nonce

    async def process_request(self, request: Request) -> Response | None:
        """Arm a per-request nonce when a policy references one; never short-circuits."""
        if self._needs_nonce:
            # One-shot factory: a request that never reads it pays only the store.
            request._state[_CSP_NONCE_STATE_KEY] = lambda: secrets.token_urlsafe(16)
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Set the enforced and report-only policy headers unless the response already has them."""
        # Field names are case-insensitive (RFC 9110 Sec. 5.1) and browsers
        # INTERSECT multiple CSP headers, so a lowercase override must suppress this.
        for header, template in (
            (HEADER_CONTENT_SECURITY_POLICY, self._enforce_template),
            (HEADER_CONTENT_SECURITY_POLICY_REPORT_ONLY, self._report_template),
        ):
            if template is None or header_present(response.headers, header):
                continue
            if "{nonce}" in template:
                nonce = csp_nonce(request)
                if nonce is None:
                    # No nonce armed: an earlier middleware answered. A fixed
                    # `nonce-None` would let an injected `<script>` run, so mint one.
                    nonce = secrets.token_urlsafe(16)
                    request._state[_CSP_NONCE_STATE_KEY] = nonce
                value = template.replace("{nonce}", f"'nonce-{nonce}'")
            else:
                value = template
            response.headers[header] = value
        return response
