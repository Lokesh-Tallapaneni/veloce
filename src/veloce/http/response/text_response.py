"""Text responses — plain text, HTML and redirects."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from veloce._constants import (
    HEADER_LOCATION,
    MIME_TEXT_PLAIN,
)
from veloce._internal import (
    MIME_HTML,
    MIME_PLAIN,
    _reject_header_crlf,
)

# ── Case-insensitive response-header access ──────────────────────────
# `Response.headers` is case-SENSITIVE but HTTP field names are not (RFC 9110
# Sec. 5.1), so these fold the lookup without allocating a dict per call.
from veloce.http.response.response import Response
from veloce.status import (
    HTTP_200_OK,
    HTTP_307_TEMPORARY_REDIRECT,
)


class _TextResponse(Response):
    """Shared base for textual responses keyed only by content type.

    `content` is sent verbatim when already `bytes`, else UTF-8 encoded.
    Subclasses set only `default_media_type`, mirroring the
    `JSONResponse.default_media_type` idiom in this module.
    """

    __slots__ = ()

    default_media_type: str = MIME_PLAIN

    def __init__(
        self,
        content: str | bytes,
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
        background: Any = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            body=content if isinstance(content, bytes) else content.encode("utf-8"),
            content_type=type(self).default_media_type,
            headers=headers,
            background=background,
        )


class HTMLResponse(_TextResponse):
    """HTML response."""

    __slots__ = ()

    default_media_type = MIME_HTML


class PlainTextResponse(_TextResponse):
    """Plain text response."""

    __slots__ = ()

    default_media_type = MIME_PLAIN


class RedirectResponse(Response):
    """HTTP redirect."""

    __slots__ = ()

    def __init__(
        self,
        url: str,
        status_code: int = HTTP_307_TEMPORARY_REDIRECT,
        headers: dict[str, str] | None = None,
    ) -> None:
        hdrs = headers or {}
        # Reject CR/LF and percent-encode, so a crafted URL or Host header cannot
        # inject response headers. The safe set keeps RFC 3986 structural
        # characters and `%`, so an encoded URL is not double-encoded.
        _reject_header_crlf(url, "redirect URL")
        hdrs[HEADER_LOCATION] = quote(url, safe="/:?#[]@!$&'()*+,;=%~")
        super().__init__(
            status_code=status_code,
            body=b"",
            content_type=MIME_TEXT_PLAIN,
            headers=hdrs,
        )
