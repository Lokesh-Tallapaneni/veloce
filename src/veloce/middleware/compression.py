"""Response compression middleware - CPU-bound work offloaded to executor."""

from __future__ import annotations

import asyncio
import contextvars
import gzip

from veloce._constants import (
    HEADER_ACCEPT_ENCODING,
    HEADER_CONTENT_ENCODING,
    HEADER_CONTENT_LENGTH,
    HEADER_CONTENT_RANGE,
    HEADER_ETAG,
    HEADER_VALUE_GZIP,
    MIME_APPLICATION_JAVASCRIPT,
    MIME_APPLICATION_X_YAML,
    MIME_APPLICATION_XHTML_XML,
    MIME_APPLICATION_XML,
    MIME_JSON,
)
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware
from veloce.status import HTTP_206_PARTIAL_CONTENT

# Default compressible content types - text formats and JSON/XML/JS.
# Image/video/audio/zip are intentionally absent: those formats already
# carry their own compression, and re-gzipping just burns CPU for no
# wire savings.
_DEFAULT_COMPRESSIBLE = (
    "text/",
    MIME_JSON,
    MIME_APPLICATION_JAVASCRIPT,
    MIME_APPLICATION_XML,
    MIME_APPLICATION_XHTML_XML,
    MIME_APPLICATION_X_YAML,
    "image/svg+xml",
)


def _accepts_gzip(accept: str) -> bool:
    """Parse Accept-Encoding and return True only if gzip is accepted (explicit or wildcard)."""
    # Fast path for the common browser shape: no header, or a list of bare
    # tokens with no q-value parameters (e.g. "gzip, deflate, br"). Only when a
    # ';' is present do we need the full parameter-aware parse below.
    if not accept:
        return False
    if ";" not in accept:
        for part in accept.split(","):
            tok = part.strip().lower()
            if tok == HEADER_VALUE_GZIP or tok == "*":
                return True
        return False
    wildcard_ok: bool | None = None
    for part in accept.split(","):
        part = part.strip()
        if not part:
            continue
        pieces = part.split(";")
        encoding = pieces[0].strip().lower()
        if encoding == HEADER_VALUE_GZIP:
            for param in pieces[1:]:
                param = param.strip()
                if param.startswith("q="):
                    try:
                        if float(param[2:]) == 0:
                            return False
                    except ValueError:
                        pass
            return True
        if encoding == "*":
            # Wildcard matches everything not explicitly listed.
            q_zero = False
            for param in pieces[1:]:
                param = param.strip()
                if param.startswith("q="):
                    try:
                        if float(param[2:]) == 0:
                            q_zero = True
                    except ValueError:
                        pass
            wildcard_ok = not q_zero
    if wildcard_ok is not None:
        return wildcard_ok
    return False


class GZipMiddleware(Middleware):
    """GZip compression for responses above a size threshold.

    Compression runs in the thread pool executor to avoid blocking the event loop.
    """

    def __init__(
        self,
        minimum_size: int = 500,
        compresslevel: int = 6,
        include_types: tuple[str, ...] | None = None,
        exclude_types: tuple[str, ...] = (),
    ) -> None:
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel
        # `include_types` is matched as a prefix (so `"text/"` covers
        # every text/* media type). None means "use the default
        # compressible set". `exclude_types` always wins on collision.
        self.include_types: tuple[str, ...] = (
            tuple(include_types) if include_types is not None else _DEFAULT_COMPRESSIBLE
        )
        self.exclude_types: tuple[str, ...] = tuple(exclude_types)

    async def process_response(self, request: Request, response: Response) -> Response:
        """Compress the response body with gzip if the client accepts it."""
        accept = request.headers.get(HEADER_ACCEPT_ENCODING, "")
        if not _accepts_gzip(accept) or len(response.body) < self.minimum_size:
            return response

        # Never compress a partial-content (206) response, or any response
        # carrying a Content-Range: gzipping changes the body bytes while
        # Content-Range / Accept-Ranges / ETag keep describing the
        # uncompressed representation, producing a protocol-invalid response
        # (RFC 9110 Sec. 14). Range responses are served whole, uncompressed.
        if (
            response.status_code == HTTP_206_PARTIAL_CONTENT
            or HEADER_CONTENT_RANGE in response.headers
        ):
            return response

        if not self._should_compress_type(response.content_type):
            return response

        # Don't re-encode a response that already declares a Content-Encoding
        # (e.g. it was returned pre-gzipped, or an upstream layer encoded it).
        # Stacking encodings produces a payload no client will decode, and
        # violates RFC 9110 Sec. 8.4 (each Content-Encoding identifies one
        # transformation; doubling them silently is a bug).
        existing_encoding = response.headers.get(HEADER_CONTENT_ENCODING)
        if existing_encoding and existing_encoding.strip().lower() not in ("", "identity"):
            return response

        # Offload CPU-bound compression to thread pool. Wrap in
        # `contextvars.copy_context().run(...)` so any ContextVar reads
        # inside the executor (today none; future-proof for hooks) see
        # the request-scoped values rather than "unbound".
        loop = asyncio.get_running_loop()
        level = self.compresslevel
        body = response.body
        ctx = contextvars.copy_context()
        compressed = await loop.run_in_executor(None, ctx.run, gzip.compress, body, level)

        if len(compressed) < len(response.body):
            response.body = compressed
            response.headers[HEADER_CONTENT_ENCODING] = HEADER_VALUE_GZIP
            response.headers[HEADER_CONTENT_LENGTH] = str(len(compressed))
            response.add_vary(HEADER_ACCEPT_ENCODING)
            response._encoded = None
            # Compression changes the bytes on the wire, so a STRONG ETag
            # (RFC 9110 Sec. 8.8.1 - byte-identical representations) no longer
            # describes them. Weaken it to `W/...`. Already-weak or malformed
            # (non-quoted) tags are left untouched so we never fabricate a
            # validator.
            etag = response.headers.get(HEADER_ETAG)
            if etag and etag[:1] == '"':
                response.headers[HEADER_ETAG] = "W/" + etag

        return response

    def _should_compress_type(self, content_type: str) -> bool:
        ct = (content_type or "").split(";", 1)[0].strip().lower()
        if not ct:
            return False
        if any(ct.startswith(p) for p in self.exclude_types):
            return False
        return any(ct.startswith(p) for p in self.include_types)
