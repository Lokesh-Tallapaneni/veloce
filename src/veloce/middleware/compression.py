"""Response compression middleware — CPU-bound work offloaded to executor."""

from __future__ import annotations

import asyncio
import contextvars
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


def _accepts_gzip(accept: str) -> bool:
    """Parse Accept-Encoding and return True only if gzip is accepted (explicit or wildcard)."""
    wildcard_ok: bool | None = None
    for part in accept.split(","):
        part = part.strip()
        if not part:
            continue
        pieces = part.split(";")
        encoding = pieces[0].strip().lower()
        if encoding == "gzip":
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
        accept = request.headers.get("accept-encoding", "")
        if not _accepts_gzip(accept) or len(response.body) < self.minimum_size:
            return response

        if not self._should_compress_type(response.content_type):
            return response

        # Don't re-encode a response that already declares a Content-Encoding
        # (e.g. it was returned pre-gzipped, or an upstream layer encoded it).
        # Stacking encodings produces a payload no client will decode, and
        # violates RFC 9110 §8.4 (each Content-Encoding identifies one
        # transformation; doubling them silently is a bug).
        existing_encoding = response.headers.get("Content-Encoding")
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
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(compressed))
            response.add_vary("Accept-Encoding")
            response._encoded = None

        return response

    def _should_compress_type(self, content_type: str) -> bool:
        ct = (content_type or "").split(";", 1)[0].strip().lower()
        if not ct:
            return False
        if any(ct.startswith(p) for p in self.exclude_types):
            return False
        return any(ct.startswith(p) for p in self.include_types)
