"""Shared string-literal constants — one canonical spelling per duplicated value.

Centralises the MIME types, Title-Case HTTP header names, and verbatim
error/log message strings that appear in two or more places across the
framework so that casing and spelling cannot drift between call sites.

These names are internal (leading-underscore module): they are not part
of the public API, are not exported from ``veloce/__init__.py``'s
``__all__``, and may change in any release. Import them inside the
framework as ``from veloce._constants import MIME_JSON``.

Casing is load-bearing. The user-facing ``Response.headers`` dict keys
are Title-Case, while the ASGI emit path uses lower-case ``bytes`` - the
``HEADER_*`` constants below hold the Title-Case ``str`` spellings ONLY
and must never be substituted where the lower-case bytes form is used.
Charset variants are distinct values and therefore distinct constants
(``MIME_TEXT_PLAIN`` is not ``MIME_TEXT_PLAIN_UTF8``).
"""

from __future__ import annotations

# ── MIME types ────────────────────────────────────────────
MIME_APPLICATION_JAVASCRIPT = "application/javascript"
MIME_APPLICATION_X_YAML = "application/x-yaml"
MIME_APPLICATION_XHTML_XML = "application/xhtml+xml"
MIME_APPLICATION_XML = "application/xml"
MIME_JSON = "application/json"
MIME_OCTET_STREAM = "application/octet-stream"
MIME_FORM_URLENCODED = "application/x-www-form-urlencoded"
MIME_MULTIPART_FORM_DATA = "multipart/form-data"
MIME_TEXT_EVENT_STREAM = "text/event-stream"
MIME_TEXT_HTML = "text/html"
MIME_TEXT_HTML_UTF8 = "text/html; charset=utf-8"
MIME_TEXT_PLAIN = "text/plain"
MIME_TEXT_PLAIN_UTF8 = "text/plain; charset=utf-8"

# ── HTTP header names (Title-Case, user-facing) ───────────
HEADER_ACCEPT = "Accept"
HEADER_ACCEPT_ENCODING = "Accept-Encoding"
HEADER_ACCEPT_CHARSET = "Accept-Charset"
HEADER_ACCEPT_LANGUAGE = "Accept-Language"
HEADER_ACCESS_CONTROL_REQUEST_HEADERS = "Access-Control-Request-Headers"
HEADER_ACCESS_CONTROL_REQUEST_METHOD = "Access-Control-Request-Method"
HEADER_ACCESS_CONTROL_REQUEST_PRIVATE_NETWORK = "Access-Control-Request-Private-Network"
HEADER_AUTHORIZATION = "Authorization"
HEADER_ACCEPT_RANGES = "Accept-Ranges"
HEADER_ACCESS_CONTROL_ALLOW_CREDENTIALS = "Access-Control-Allow-Credentials"
HEADER_ACCESS_CONTROL_ALLOW_HEADERS = "Access-Control-Allow-Headers"
HEADER_ACCESS_CONTROL_ALLOW_METHODS = "Access-Control-Allow-Methods"
HEADER_ACCESS_CONTROL_ALLOW_ORIGIN = "Access-Control-Allow-Origin"
HEADER_ACCESS_CONTROL_ALLOW_PRIVATE_NETWORK = "Access-Control-Allow-Private-Network"
HEADER_ACCESS_CONTROL_EXPOSE_HEADERS = "Access-Control-Expose-Headers"
HEADER_ACCESS_CONTROL_MAX_AGE = "Access-Control-Max-Age"
HEADER_AGE = "Age"
HEADER_ALLOW = "Allow"
HEADER_CACHE_CONTROL = "Cache-Control"
HEADER_COOKIE = "Cookie"
HEADER_CONNECTION = "Connection"
HEADER_CONTENT_DISPOSITION = "Content-Disposition"
HEADER_CONTENT_ENCODING = "Content-Encoding"
HEADER_CONTENT_LANGUAGE = "Content-Language"
HEADER_CONTENT_LENGTH = "Content-Length"
HEADER_CONTENT_LOCATION = "Content-Location"
HEADER_CONTENT_RANGE = "Content-Range"
HEADER_CONTENT_SECURITY_POLICY = "Content-Security-Policy"
HEADER_CONTENT_SECURITY_POLICY_REPORT_ONLY = "Content-Security-Policy-Report-Only"
HEADER_CONTENT_TYPE = "Content-Type"
HEADER_DATE = "Date"
HEADER_ETAG = "ETag"
HEADER_EXPIRES = "Expires"
HEADER_HOST = "Host"
HEADER_IF_MATCH = "If-Match"
HEADER_IF_MODIFIED_SINCE = "If-Modified-Since"
HEADER_IF_NONE_MATCH = "If-None-Match"
HEADER_IF_RANGE = "If-Range"
HEADER_IF_UNMODIFIED_SINCE = "If-Unmodified-Since"
HEADER_LAST_MODIFIED = "Last-Modified"
HEADER_LOCATION = "Location"
HEADER_MAX_FORWARDS = "Max-Forwards"
HEADER_ORIGIN = "Origin"
HEADER_PERMISSIONS_POLICY = "Permissions-Policy"
HEADER_PRAGMA = "Pragma"
HEADER_RANGE = "Range"
HEADER_REFERER = "Referer"
HEADER_REFERRER_POLICY = "Referrer-Policy"
HEADER_RETRY_AFTER = "Retry-After"
HEADER_SEC_WEBSOCKET_KEY = "Sec-WebSocket-Key"
HEADER_SEC_WEBSOCKET_PROTOCOL = "Sec-WebSocket-Protocol"
HEADER_SET_COOKIE = "Set-Cookie"
HEADER_STRICT_TRANSPORT_SECURITY = "Strict-Transport-Security"
HEADER_TRANSFER_ENCODING = "Transfer-Encoding"
HEADER_USER_AGENT = "User-Agent"
HEADER_VARY = "Vary"
HEADER_WWW_AUTHENTICATE = "WWW-Authenticate"
HEADER_X_ACCEL_BUFFERING = "X-Accel-Buffering"
HEADER_X_CONTENT_TYPE_OPTIONS = "X-Content-Type-Options"
HEADER_X_CSRF_TOKEN = "X-CSRF-Token"
HEADER_X_FORWARDED_FOR = "X-Forwarded-For"
HEADER_X_FORWARDED_HOST = "X-Forwarded-Host"
HEADER_X_FORWARDED_PORT = "X-Forwarded-Port"
HEADER_X_FORWARDED_PREFIX = "X-Forwarded-Prefix"
HEADER_X_FORWARDED_PROTO = "X-Forwarded-Proto"
HEADER_X_FRAME_OPTIONS = "X-Frame-Options"
HEADER_X_RATELIMIT_LIMIT = "X-RateLimit-Limit"
HEADER_X_RATELIMIT_REMAINING = "X-RateLimit-Remaining"
HEADER_X_RATELIMIT_RESET = "X-RateLimit-Reset"
HEADER_X_REQUEST_ID = "X-Request-ID"
HEADER_X_REQUESTED_WITH = "X-Requested-With"

# ── HTTP header values (industry-standard, reusable) ──────
HEADER_VALUE_ATTACHMENT = "attachment"
HEADER_VALUE_BYTES = "bytes"
HEADER_VALUE_CHUNKED = "chunked"
HEADER_VALUE_DENY = "DENY"
HEADER_VALUE_GZIP = "gzip"
HEADER_VALUE_KEEP_ALIVE = "keep-alive"
HEADER_VALUE_NO_CACHE = "no-cache"
HEADER_VALUE_NOSNIFF = "nosniff"
HEADER_VALUE_PUBLIC = "public"
HEADER_VALUE_STRICT_ORIGIN_WHEN_CROSS_ORIGIN = "strict-origin-when-cross-origin"

# ── Duplicated messages and error/log strings ─────────────
MSG_ACCESS_DENIED = "Access denied"
MSG_APP_REFERENCE_FORM = "App reference in 'module:attribute' form."
MSG_ERROR_RESPONSE_EMISSION = "Error during response emission"
MSG_FIELD_REQUIRED = "field required"
MSG_INTERNAL_SERVER_ERROR = "Internal Server Error"
MSG_INVALID_QUERY_STRING = "Invalid query string encoding"
MSG_METHOD_NOT_ALLOWED = "Method Not Allowed"
MSG_NOT_AUTHENTICATED = "Not authenticated"
MSG_NOT_FOUND = "Not Found"
MSG_RECEIVER_RAISED = "Receiver %r for signal %r raised %s"
MSG_REQUEST_BODY_EXCEEDS_MAX = "Request body exceeds MAX_CONTENT_LENGTH"
MSG_SUCCESSFUL_RESPONSE = "Successful Response"

# ── Duplicated CRLF-rejection context labels ──────────────
MSG_LABEL_COOKIE_DOMAIN = "cookie domain"
MSG_LABEL_COOKIE_NAME = "cookie name"
MSG_LABEL_COOKIE_PATH = "cookie path"
MSG_LABEL_COOKIE_SAMESITE = "cookie samesite"
MSG_LABEL_COOKIE_VALUE = "cookie value"
MSG_LABEL_HEADER_NAME = "header name"
MSG_LABEL_SET_COOKIE_VALUE = "Set-Cookie value"
