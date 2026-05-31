"""Shared string-literal constants — one canonical spelling per duplicated value.

Centralises the MIME types, Title-Case HTTP header names, and verbatim
error/log message strings that appear in two or more places across the
framework so that casing and spelling cannot drift between call sites.

These names are internal (leading-underscore module): they are not part
of the public API, are not exported from ``veloce/__init__.py``'s
``__all__``, and may change in any release. Import them inside the
framework as ``from veloce._constants import MIME_JSON``.

Casing is load-bearing. The user-facing ``Response.headers`` dict keys
are Title-Case, while the ASGI emit path uses lower-case ``bytes`` — the
``HEADER_*`` constants below hold the Title-Case ``str`` spellings ONLY
and must never be substituted where the lower-case bytes form is used.
Charset variants are distinct values and therefore distinct constants
(``MIME_TEXT_PLAIN`` is not ``MIME_TEXT_PLAIN_UTF8``).
"""

from __future__ import annotations

# ── MIME types ──
MIME_JSON = "application/json"
MIME_OCTET_STREAM = "application/octet-stream"
MIME_FORM_URLENCODED = "application/x-www-form-urlencoded"
MIME_MULTIPART_FORM_DATA = "multipart/form-data"
MIME_TEXT_HTML = "text/html"
MIME_TEXT_HTML_UTF8 = "text/html; charset=utf-8"
MIME_TEXT_PLAIN = "text/plain"
MIME_TEXT_PLAIN_UTF8 = "text/plain; charset=utf-8"

# ── HTTP header names (Title-Case, user-facing) ──
HEADER_ACCEPT_RANGES = "Accept-Ranges"
HEADER_ACCESS_CONTROL_ALLOW_HEADERS = "Access-Control-Allow-Headers"
HEADER_CACHE_CONTROL = "Cache-Control"
HEADER_CONTENT_DISPOSITION = "Content-Disposition"
HEADER_CONTENT_ENCODING = "Content-Encoding"
HEADER_CONTENT_LANGUAGE = "Content-Language"
HEADER_CONTENT_LENGTH = "Content-Length"
HEADER_CONTENT_LOCATION = "Content-Location"
HEADER_CONTENT_RANGE = "Content-Range"
HEADER_CONTENT_TYPE = "Content-Type"
HEADER_ETAG = "ETag"
HEADER_LAST_MODIFIED = "Last-Modified"
HEADER_RETRY_AFTER = "Retry-After"
HEADER_SET_COOKIE = "Set-Cookie"
HEADER_TRANSFER_ENCODING = "Transfer-Encoding"
HEADER_WWW_AUTHENTICATE = "WWW-Authenticate"

# ── Duplicated messages and error/log strings ──
MSG_ACCESS_DENIED = "Access denied"
MSG_APP_REFERENCE_FORM = "App reference in 'module:attribute' form."
MSG_ERROR_RESPONSE_EMISSION = "Error during response emission"
MSG_FIELD_REQUIRED = "field required"
MSG_INTERNAL_SERVER_ERROR = "Internal Server Error"
MSG_METHOD_NOT_ALLOWED = "Method Not Allowed"
MSG_NOT_AUTHENTICATED = "Not authenticated"
MSG_NOT_FOUND = "Not Found"
MSG_RECEIVER_RAISED = "Receiver %r for signal %r raised %s"
MSG_REQUEST_BODY_EXCEEDS_MAX = "Request body exceeds MAX_CONTENT_LENGTH"
MSG_SUCCESSFUL_RESPONSE = "Successful Response"

# ── Duplicated CRLF-rejection context labels ──
MSG_LABEL_COOKIE_DOMAIN = "cookie domain"
MSG_LABEL_COOKIE_NAME = "cookie name"
MSG_LABEL_COOKIE_PATH = "cookie path"
MSG_LABEL_COOKIE_SAMESITE = "cookie samesite"
MSG_LABEL_COOKIE_VALUE = "cookie value"
MSG_LABEL_HEADER_NAME = "header name"
MSG_LABEL_SET_COOKIE_VALUE = "Set-Cookie value"
