"""Security-related middleware — the six hardening layers and their helper.

`CSPMiddleware` emits a Content Security Policy and, with `csp_nonce()`, a
per-request nonce a template can put on an inline script.
`TrustedHostMiddleware` validates Host (RFC 9110 Sec. 7.2),
`RateLimitMiddleware` emits rate-limit headers
(draft-ietf-httpapi-ratelimit-headers), `HTTPSRedirectMiddleware` upgrades with
a 308 (RFC 9110 Sec. 15.4.9), `SecurityHeadersMiddleware` sets the common
hardening response headers, and `WebSocketOriginMiddleware` checks Origin
against CSWSH (RFC 6455 Sec. 4.1, Sec. 4.2.2).
"""

from __future__ import annotations

from veloce.middleware.security.csp import (
    _CSP_NONCE_STATE_KEY,
    CSPMiddleware,
    _normalize_csp,
    csp_nonce,
)
from veloce.middleware.security.https_redirect import HTTPSRedirectMiddleware
from veloce.middleware.security.rate_limit import (
    _RL_STATE_KEY,
    _SUPPLIED_KEY_PREFIX,
    RateLimitMiddleware,
    _SlidingLog,
)
from veloce.middleware.security.security_headers import SecurityHeadersMiddleware
from veloce.middleware.security.trusted_host import TrustedHostMiddleware
from veloce.middleware.security.websocket_origin import WebSocketOriginMiddleware

__all__ = [
    "CSPMiddleware",
    "HTTPSRedirectMiddleware",
    "RateLimitMiddleware",
    "SecurityHeadersMiddleware",
    "TrustedHostMiddleware",
    "WebSocketOriginMiddleware",
    "csp_nonce",
]
