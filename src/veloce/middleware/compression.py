"""Response compression middleware - CPU-bound work offloaded to executor."""

from __future__ import annotations

import asyncio
import contextvars
import gzip
import zlib
from typing import TYPE_CHECKING, Any

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
    MIME_TEXT_EVENT_STREAM,
)
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware
from veloce.status import HTTP_206_PARTIAL_CONTENT

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import AsyncIterator

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
        min_stream_chunk_offload: int = 32768,
        latency_sensitive_types: frozenset[str] = frozenset({MIME_TEXT_EVENT_STREAM}),
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
        # Streaming bodies are compressed chunk-by-chunk through a single
        # `zlib.compressobj`. A chunk at or above this many bytes is offloaded
        # to the thread-pool executor (CPU-bound); smaller frames compress
        # inline to avoid task-scheduling overhead on the common case.
        self.min_stream_chunk_offload = min_stream_chunk_offload
        # Bare content types that must never be buffered through compression:
        # SSE (`text/event-stream`) trades wire size for per-event latency, and
        # routing it through `compressobj` would merge/delay events.
        self.latency_sensitive_types = latency_sensitive_types

    async def process_response(self, request: Request, response: Response) -> Response:
        """Compress the response body with gzip if the client accepts it."""
        accept = request.headers.get(HEADER_ACCEPT_ENCODING, "")
        if not _accepts_gzip(accept):
            return response

        # Streaming bodies have no materialised `response.body`, so the
        # `minimum_size` gate (a buffered-only heuristic) does not apply.
        # Compress the stream lazily, chunk-by-chunk, and fall through to the
        # buffered path only for non-streamed responses.
        if response.is_streamed:
            return self._process_stream(request, response)

        if len(response.body) < self.minimum_size:
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
            self._weaken_strong_etag(response)

        return response

    def _process_stream(self, request: Request, response: Response) -> Response:
        """Wrap a streaming response's body in a lazy gzip compressor.

        Mirrors the buffered guards (compressible type, no pre-existing
        encoding, no 206 / Content-Range) but skips real-time latency-sensitive
        streams (SSE) so events are not buffered through `compressobj`.
        """
        # SSE and other latency-sensitive streams trade wire size for
        # per-event delivery; routing them through a buffering compressor would
        # merge or delay frames.
        if (
            getattr(response, "is_event_source", False)
            or response.mimetype in self.latency_sensitive_types
        ):
            return response

        if not self._should_compress_type(response.content_type):
            return response

        # Don't re-encode a response that already declares a Content-Encoding.
        existing_encoding = response.headers.get(HEADER_CONTENT_ENCODING)
        if existing_encoding and existing_encoding.strip().lower() not in ("", "identity"):
            return response

        # Range responses are served whole and uncompressed (see the buffered
        # path for the RFC 9110 Sec. 14 rationale).
        if (
            response.status_code == HTTP_206_PARTIAL_CONTENT
            or HEADER_CONTENT_RANGE in response.headers
        ):
            return response

        response._stream = self._compress_stream(response._stream, request)
        response.headers[HEADER_CONTENT_ENCODING] = HEADER_VALUE_GZIP
        # A streamed gzip body is chunked / `more_body`-framed; any declared
        # length describes the uncompressed representation and must go (the
        # native chunked path relies on Transfer-Encoding, not Content-Length).
        response.headers.pop(HEADER_CONTENT_LENGTH, None)
        response.headers.pop("content-length", None)
        response.add_vary(HEADER_ACCEPT_ENCODING)
        response._encoded = None
        self._weaken_strong_etag(response)
        return response

    async def _compress_stream(self, stream: Any, request: Request) -> AsyncIterator[bytes]:
        """Gzip a chunk stream lazily, reusing one `compressobj` across chunks.

        `wbits = MAX_WBITS | 16` selects gzip framing (header + CRC trailer) so
        the emitted bytes match `Content-Encoding: gzip`, like the buffered
        path's `gzip.compress`. Compression is deferred (no per-chunk flush) so
        the stream stays well-compressed; the gzip trailer is written by the
        final `Z_FINISH` flush.
        """
        co = zlib.compressobj(self.compresslevel, zlib.DEFLATED, zlib.MAX_WBITS | 16)
        loop = asyncio.get_running_loop()
        async for chunk in stream:
            # Downstream chunked / ASGI emit paths expect bytes; streams may
            # yield str (see `StreamingResponse._aiter_sync`).
            b = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
            if len(b) < self.min_stream_chunk_offload:
                out = co.compress(b)
            else:
                # Offload large frames to the executor, preserving ContextVars.
                ctx = contextvars.copy_context()
                out = await loop.run_in_executor(None, ctx.run, co.compress, b)
            if out:
                yield out
        # `Z_FINISH` emits any buffered output plus the gzip CRC/length trailer.
        tail = co.flush(zlib.Z_FINISH)
        if tail:
            yield tail

    def _weaken_strong_etag(self, response: Response) -> None:
        """Downgrade a strong ETag to weak after the wire bytes change.

        Compression changes the bytes on the wire, so a STRONG ETag
        (RFC 9110 Sec. 8.8.1 - byte-identical representations) no longer
        describes them. Already-weak or malformed (non-quoted) tags are left
        untouched so we never fabricate a validator. `headers` is a plain dict,
        so accept either spelling and rewrite whichever key holds the tag.
        """
        for etag_key in (HEADER_ETAG, "etag"):
            etag = response.headers.get(etag_key)
            if etag and etag[:1] == '"':
                response.headers[etag_key] = "W/" + etag
                break

    def _should_compress_type(self, content_type: str) -> bool:
        ct = (content_type or "").split(";", 1)[0].strip().lower()
        if not ct:
            return False
        if any(ct.startswith(p) for p in self.exclude_types):
            return False
        return any(ct.startswith(p) for p in self.include_types)
