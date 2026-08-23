"""Response compression middleware — CPU-bound work offloaded to executor."""

from __future__ import annotations

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
from veloce._internal import offload
from veloce.http.request import Request
from veloce.http.response import Response, header_get, header_key, header_present
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

    Usage::

        app.add_middleware(GZipMiddleware(minimum_size=1024, compresslevel=6))
    """

    def __init__(
        self,
        minimum_size: int = 500,
        compresslevel: int = 6,
        include_types: tuple[str, ...] | None = None,
        exclude_types: tuple[str, ...] = (),
        min_stream_chunk_offload: int = 32768,
        latency_sensitive_types: frozenset[str] = frozenset({MIME_TEXT_EVENT_STREAM}),
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
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
        # The buffered path uses the same threshold: the question it answers -
        # "is this body big enough that the pool beats holding the loop?" - is
        # the same one, so a caller who tuned it for streams gets the tuning
        # applied consistently rather than only to half the middleware.
        self.min_offload_size = min_stream_chunk_offload
        # Bare content types that must never be buffered through compression:
        # SSE (`text/event-stream`) trades wire size for per-event latency, and
        # routing it through `compressobj` would merge/delay events.
        self.latency_sensitive_types = latency_sensitive_types

    async def process_response(self, request: Request, response: Response) -> Response:
        """Compress the response body with gzip if the client accepts it."""
        # This middleware negotiates content on Accept-Encoding, so every
        # response it touches varies on that header - including the ones it
        # leaves uncompressed (no gzip in Accept-Encoding, below `minimum_size`,
        # a 206 / Content-Range, an incompressible type, an already-encoded
        # body). Declaring `Vary: Accept-Encoding` up front, before any
        # short-circuit, keeps the encoding dimension visible to every cache
        # layer end-to-end (RFC 9110 Sec. 12.5.5) rather than only on the
        # compressed path. The streamed path is reached via the delegation
        # below, so it inherits this marker too; `add_vary` de-duplicates, so
        # the success path does not re-add it.
        response.add_vary(HEADER_ACCEPT_ENCODING)
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
        if response.status_code == HTTP_206_PARTIAL_CONTENT or header_present(
            response.headers, HEADER_CONTENT_RANGE
        ):
            return response

        if self._skip_for_type_or_encoding(response):
            return response

        # Offloading is not free: it costs a handoff per response, and under
        # load every compressing request queues on the same pool. Compressing
        # inline holds the loop, which is the cost that matters - but for a
        # small body that hold is short, while the handoff is paid in full.
        #
        # Measured at 32 concurrent requests, completed requests per second,
        # inline vs offloaded: 2 KiB 15795 vs 5684, 6 KiB 12489 vs 5393,
        # 32 KiB 5432 vs 4102, 48 KiB 4043 vs 4101, 128 KiB 1348 vs 2814,
        # 512 KiB 316 vs 1045. The crossover is near 48 KiB: below it the
        # handoff and pool contention dominate, above it zlib releases the GIL
        # for long enough that the pool genuinely parallelises and the loop is
        # better off free. The threshold sits below the crossover deliberately,
        # so the offload starts before inline compression becomes the slower
        # choice. It is the threshold the streaming path already applies per
        # chunk.
        level = self.compresslevel
        body = response.body
        if len(body) < self.min_offload_size:
            compressed = gzip.compress(body, level)
        else:
            compressed = await offload(gzip.compress, body, level)

        clen = len(compressed)
        if clen < len(response.body):
            response.body = compressed
            self._finalize_gzip_headers(response, content_length=clen)

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

        if self._skip_for_type_or_encoding(response):
            return response

        # Range responses are served whole and uncompressed (see the buffered
        # path for the RFC 9110 Sec. 14 rationale).
        if response.status_code == HTTP_206_PARTIAL_CONTENT or header_present(
            response.headers, HEADER_CONTENT_RANGE
        ):
            return response

        response._stream = self._compress_stream(response._stream, request)
        # A streamed gzip body is chunked / `more_body`-framed; any declared
        # length describes the uncompressed representation, so finalize with no
        # Content-Length (the native chunked path relies on Transfer-Encoding).
        self._finalize_gzip_headers(response, content_length=None)
        return response

    def _skip_for_type_or_encoding(self, response: Response) -> bool:
        """Return True when the response must not be gzipped on type/encoding grounds.

        Shared by the buffered and streaming paths: a non-compressible content
        type, or a response that already declares a non-identity
        Content-Encoding, is passed through untouched. Stacking encodings
        produces a payload no client will decode and violates RFC 9110 Sec. 8.4
        (each Content-Encoding identifies one transformation; doubling them is a
        bug). Field names are case-insensitive (RFC 9110 Sec. 5.1), so any
        casing is honored.
        """
        if not self._should_compress_type(response.content_type):
            return True
        existing_encoding = header_get(response.headers, HEADER_CONTENT_ENCODING)
        if not existing_encoding:
            return False
        return existing_encoding.strip().lower() not in ("", "identity")

    def _finalize_gzip_headers(self, response: Response, content_length: int | None) -> None:
        """Rewrite headers after the body bytes have been gzipped.

        Field names are case-insensitive (RFC 9110 Sec. 5.1): a handler may have
        stored Content-Encoding / Content-Length under any casing, so drop every
        existing spelling first and write the canonical key once - otherwise a
        stale mixed-case length would describe the uncompressed body.
        `content_length` is the compressed byte count for the buffered path, or
        `None` for the streaming path where the chunked/`more_body` framing
        carries no declared length.
        """
        self._drop_header(response, HEADER_CONTENT_ENCODING)
        self._drop_header(response, HEADER_CONTENT_LENGTH)
        response.headers[HEADER_CONTENT_ENCODING] = HEADER_VALUE_GZIP
        if content_length is not None:
            response.headers[HEADER_CONTENT_LENGTH] = str(content_length)
        # `Vary: Accept-Encoding` is already set by `process_response` at entry,
        # ahead of every short-circuit, so it is not re-added here.
        response._encoded = None
        self._weaken_strong_etag(response)

    @staticmethod
    def _drop_header(response: Response, name: str) -> None:
        """Remove every casing of `name` from the response headers.

        Field names are case-insensitive (RFC 9110 Sec. 5.1), so a handler may
        have stored the header under any spelling. `header_key` resolves the
        actual stored key; loop until none remain to also clear accidental
        duplicates before the compression path writes its canonical value.
        """
        while (key := header_key(response.headers, name)) is not None:
            del response.headers[key]

    @staticmethod
    def _compress_frame(co: Any, b: bytes) -> bytes:
        """Compress one input chunk into a self-contained, deliverable frame.

        zlib/DEFLATE buffers internally (RFC 1951): `compress()` alone may
        return nothing until enough input accumulates, so a long-lived chunked
        stream (NDJSON, log tails) would stall at the gzip header until EOF.
        `flush(Z_SYNC_FLUSH)` forces the codec to emit everything buffered so
        far, terminated by an empty stored block, without resetting the
        compression context - so each input chunk yields output the client can
        decode incrementally while later chunks still benefit from the shared
        dictionary. Run as one unit so an offloaded chunk does both steps in
        the executor.
        """
        return co.compress(b) + co.flush(zlib.Z_SYNC_FLUSH)

    async def _compress_stream(self, stream: Any, request: Request) -> AsyncIterator[bytes]:
        """Gzip a chunk stream lazily, reusing one `compressobj` across chunks.

        `wbits = MAX_WBITS | 16` selects gzip framing (header + CRC trailer) so
        the emitted bytes match `Content-Encoding: gzip`, like the buffered
        path's `gzip.compress`. Each input chunk is sync-flushed into its own
        deliverable frame (see `_compress_frame`); the gzip trailer is written
        by the final `Z_FINISH` flush.
        """
        co = zlib.compressobj(self.compresslevel, zlib.DEFLATED, zlib.MAX_WBITS | 16)
        async for chunk in stream:
            # Downstream chunked / ASGI emit paths expect bytes; streams may
            # yield str (see `StreamingResponse._aiter_sync`).
            b = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
            if len(b) < self.min_stream_chunk_offload:
                out = self._compress_frame(co, b)
            else:
                # Offload large frames to the thread pool, preserving ContextVars.
                out = await offload(self._compress_frame, co, b)
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
        # Find the actual stored key (RFC 9110 Sec. 5.1 - field names are
        # case-insensitive, so a handler-set `Etag`/`ETAG` must be located)
        # and rewrite the strong validator weak in place under that same key.
        etag_key = header_key(response.headers, HEADER_ETAG)
        if etag_key is not None:
            etag = response.headers[etag_key]
            if etag and etag[:1] == '"':
                response.headers[etag_key] = "W/" + etag

    def _should_compress_type(self, content_type: str) -> bool:
        ct = (content_type or "").split(";", 1)[0].strip().lower()
        if not ct:
            return False
        if any(ct.startswith(p) for p in self.exclude_types):
            return False
        return any(ct.startswith(p) for p in self.include_types)
