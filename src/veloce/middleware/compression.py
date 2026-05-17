"""Response compression middleware — CPU-bound work offloaded to executor."""

from __future__ import annotations

import asyncio
import gzip

from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware

# Default compressible content types — text formats and JSON/XML/JS.
# Image/video/audio/zip are intentionally absent: those formats already
# carry their own compression, and re-gzipping just burns CPU for no
# wire savings.
_DEFAULT_COMPRESSIBLE = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "application/xhtml+xml",
    "application/x-yaml",
    "image/svg+xml",
)


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

    def _should_compress_type(self, content_type: str) -> bool:
        ct = (content_type or "").split(";", 1)[0].strip().lower()
        if not ct:
            return False
        if any(ct.startswith(p) for p in self.exclude_types):
            return False
        return any(ct.startswith(p) for p in self.include_types)

    async def process_response(self, request: Request, response: Response) -> Response:
        accept = request.headers.get("accept-encoding", "")
        if "gzip" not in accept or len(response.body) < self.minimum_size:
            return response

        if not self._should_compress_type(response.content_type):
            return response

        # Don't re-encode a response that already declares a Content-Encoding
        # (e.g. it was returned pre-gzipped, or an upstream layer encoded it).
        # Stacking encodings produces a payload no client will decode, and
        # violates RFC 9110 §8.4 (each Content-Encoding identifies one
        # transformation; doubling them silently is a bug).
        existing_encoding = response.headers.get("Content-Encoding") or response.headers.get(
            "content-encoding"
        )
        if existing_encoding and existing_encoding.strip().lower() not in ("", "identity"):
            return response

        # Offload CPU-bound compression to thread pool
        loop = asyncio.get_running_loop()
        level = self.compresslevel
        body = response.body
        compressed = await loop.run_in_executor(
            None, lambda: gzip.compress(body, compresslevel=level)
        )

        if len(compressed) < len(response.body):
            response.body = compressed
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(compressed))
            # Add `Accept-Encoding` to `Vary` so caches key by the negotiated
            # encoding (per RFC 9110 §12.5.5 / §15.5.5).
            existing_vary = response.headers.get("Vary") or response.headers.get("vary")
            if existing_vary:
                tokens = {t.strip().lower() for t in existing_vary.split(",")}
                if "accept-encoding" not in tokens:
                    response.headers["Vary"] = existing_vary + ", Accept-Encoding"
            else:
                response.headers["Vary"] = "Accept-Encoding"
            response._encoded = None

        return response
