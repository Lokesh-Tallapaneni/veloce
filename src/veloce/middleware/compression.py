"""Response compression middleware — CPU-bound work offloaded to executor."""

from __future__ import annotations

import gzip
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from veloce._constants import (
    HEADER_ACCEPT_ENCODING,
    HEADER_CONTENT_ENCODING,
    HEADER_CONTENT_LENGTH,
    HEADER_CONTENT_RANGE,
    HEADER_ETAG,
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


@dataclass(frozen=True, slots=True)
class _Codec:
    """One content coding: how to compress a whole body, and a stream of chunks.

    `stream` returns an object exposing `frame(bytes) -> bytes` and
    `finish() -> bytes`. Every codec buffers internally, so `frame` must flush
    whatever the codec is holding into output the client can decode on arrival -
    otherwise a long-lived chunked stream stalls until EOF - without ending the
    compression context, so later chunks keep the shared dictionary.
    """

    name: str
    package: str
    default_level: int
    compress: Callable[[bytes, int], bytes]
    stream: Callable[[int], Any]


class _ZlibStream:
    """Gzip framing over `zlib.compressobj` (`MAX_WBITS | 16`)."""

    __slots__ = ("_co",)

    def __init__(self, level: int) -> None:
        self._co = zlib.compressobj(level, zlib.DEFLATED, zlib.MAX_WBITS | 16)

    def frame(self, data: bytes) -> bytes:
        # `Z_SYNC_FLUSH` emits everything buffered, terminated by an empty
        # stored block, without resetting the context (RFC 1951).
        return self._co.compress(data) + self._co.flush(zlib.Z_SYNC_FLUSH)

    def finish(self) -> bytes:
        # `Z_FINISH` writes the gzip CRC/length trailer.
        return self._co.flush(zlib.Z_FINISH)


def _gzip_codec() -> _Codec:
    return _Codec("gzip", "", 6, lambda data, level: gzip.compress(data, level), _ZlibStream)


def _brotli_codec() -> _Codec | None:
    try:
        import brotli
    except ImportError:
        try:
            import brotlicffi as brotli
        except ImportError:
            return None

    class _BrotliStream:
        __slots__ = ("_co",)

        def __init__(self, level: int) -> None:
            self._co = brotli.Compressor(quality=level)

        def frame(self, data: bytes) -> bytes:
            return self._co.process(data) + self._co.flush()

        def finish(self) -> bytes:
            return self._co.finish()

    # Quality 4, not the library default of 11. Brotli's top qualities trade
    # orders of magnitude of CPU for a few percent of ratio - a setting for
    # assets compressed once at build time, not for a response being produced
    # now. Around 4 it is competitive with gzip on speed and better on ratio.
    return _Codec(
        "br", "brotli", 4, lambda data, level: brotli.compress(data, quality=level), _BrotliStream
    )


def _zstd_codec() -> _Codec | None:
    try:
        import zstandard
    except ImportError:
        return None

    class _ZstdStream:
        __slots__ = ("_co",)

        def __init__(self, level: int) -> None:
            self._co = zstandard.ZstdCompressor(level=level).compressobj()

        def frame(self, data: bytes) -> bytes:
            return self._co.compress(data) + self._co.flush(zstandard.COMPRESSOBJ_FLUSH_BLOCK)

        def finish(self) -> bytes:
            return self._co.flush(zstandard.COMPRESSOBJ_FLUSH_FINISH)

    return _Codec(
        "zstd",
        "zstandard",
        3,
        lambda data, level: zstandard.ZstdCompressor(level=level).compress(data),
        _ZstdStream,
    )


# Resolved once at import: the optional packages are imported here or not at all,
# so a per-response path never pays an import attempt. A `None` entry means the
# coding is known but its package is absent.
_CODECS: dict[str, _Codec | None] = {
    "zstd": _zstd_codec(),
    "br": _brotli_codec(),
    "gzip": _gzip_codec(),
}

# Server preference when the caller names none: best ratio first. Every entry is
# filtered against what is installed, and gzip is always there.
_DEFAULT_ALGORITHMS = ("zstd", "br", "gzip")

# Package to install for each optional coding, for the error a caller sees when
# the only coding they asked for has none.
_CODECS_PACKAGE = {"zstd": "zstandard", "br": "brotli", "gzip": "gzip (stdlib)"}


def _refuses(pieces: list[str]) -> bool:
    """True when a media-range's parameters carry `q=0`.

    Both spellings: RFC 9110 Sec. 12.4.2's `weight` rule is written with an ABNF
    string literal, and RFC 5234 Sec. 2.3 makes those case-insensitive, so `Q=0`
    is as much a refusal as `q=0`. `AcceptHeader` already reads both; this is the
    same rule, applied once for the explicit media range and the wildcard.
    """
    for param in pieces[1:]:
        param = param.strip()
        if param[:2] in ("q=", "Q="):
            try:
                if float(param[2:]) == 0:
                    return True
            except ValueError:
                pass
    return False


def _quality(pieces: list[str]) -> float:
    """The `q` weight of one media range, defaulting to 1 (RFC 9110 Sec. 12.4.2).

    Both spellings, since RFC 5234 Sec. 2.3 makes the ABNF literal
    case-insensitive. A malformed weight is ignored rather than fatal - a header
    is client input, and the conservative reading of an unparseable weight is
    the default the client would have got by omitting it.
    """
    for param in pieces[1:]:
        param = param.strip()
        if param[:2] in ("q=", "Q="):
            try:
                return float(param[2:])
            except ValueError:
                return 1.0
    return 1.0


def _negotiate(accept: str, offered: tuple[str, ...]) -> str | None:
    """Pick the content coding to use, or `None` to send the body unencoded.

    The client's weights rank the candidates and the server's `offered` order
    breaks ties, so a client that states a preference gets it and one that lists
    bare tokens gets the deployment's choice. `q=0` is a refusal; `*` supplies a
    weight for any coding the header does not name.
    """
    if not accept:
        return None

    # Fast path for the shape browsers actually send - bare tokens, no weights -
    # where the answer is simply the first offered coding the client listed.
    if ";" not in accept:
        listed = {part.strip().lower() for part in accept.split(",")}
        if "*" in listed:
            return offered[0]
        for coding in offered:
            if coding in listed:
                return coding
        return None

    weights: dict[str, float] = {}
    wildcard: float | None = None
    for part in accept.split(","):
        part = part.strip()
        if not part:
            continue
        pieces = part.split(";")
        coding = pieces[0].strip().lower()
        if coding == "*":
            wildcard = _quality(pieces)
        else:
            weights[coding] = _quality(pieces)

    best: str | None = None
    best_weight = 0.0
    for coding in offered:
        weight = weights.get(coding, wildcard if wildcard is not None else 0.0)
        # Strictly greater, so `offered` order breaks a tie: the first coding at
        # the winning weight is the server's most preferred one.
        if weight > best_weight:
            best, best_weight = coding, weight
    return best


class CompressionMiddleware(Middleware):
    """Response compression, negotiated over the codings the server can emit.

    Offers zstd, brotli and gzip - whichever of their packages are installed -
    and picks one per response from `Accept-Encoding`. The client's `q` weights
    rank the candidates; `algorithms` order breaks ties, so a deployment states
    its own preference for clients that express none.

    Compression above `min_stream_chunk_offload` bytes runs in the thread pool
    to avoid holding the event loop.

    Usage::

        from veloce import CompressionMiddleware

        app.add_middleware(CompressionMiddleware(algorithms=("br", "gzip")))
    """

    def __init__(
        self,
        minimum_size: int = 500,
        compresslevel: int | None = None,
        include_types: tuple[str, ...] | None = None,
        exclude_types: tuple[str, ...] = (),
        min_stream_chunk_offload: int = 32768,
        latency_sensitive_types: frozenset[str] = frozenset({MIME_TEXT_EVENT_STREAM}),
        name: str | None = None,
        *,
        algorithms: tuple[str, ...] | None = None,
        levels: dict[str, int] | None = None,
    ) -> None:
        super().__init__(name=name)
        self.minimum_size = minimum_size
        self.algorithms, self.levels = self._resolve_codings(algorithms, levels, compresslevel)
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
        # One threshold for both halves. A buffered body and a streamed chunk
        # ask the same question - "is this big enough that the pool beats
        # holding the loop?" - so a caller who tunes it gets it applied to both
        # rather than to half the middleware.
        self.min_stream_chunk_offload = min_stream_chunk_offload
        # Bare content types that must never be buffered through compression:
        # SSE (`text/event-stream`) trades wire size for per-event latency, and
        # routing it through `compressobj` would merge/delay events.
        self.latency_sensitive_types = latency_sensitive_types

    #: Codings this class may offer. `GZipMiddleware` narrows it to gzip.
    _supported: tuple[str, ...] = _DEFAULT_ALGORITHMS

    @classmethod
    def _resolve_codings(
        cls,
        algorithms: tuple[str, ...] | None,
        levels: dict[str, int] | None,
        compresslevel: int | None,
    ) -> tuple[tuple[str, ...], dict[str, int]]:
        """Validate the requested codings and settle a level for each.

        An algorithm whose package is missing is dropped rather than made fatal:
        a deployment that lists three codings and installs two should serve the
        two. Asking *only* for a missing one is different - there is nothing left
        to compress with, and silently serving plaintext would hide it - so that
        raises, naming the package to install.
        """
        requested = tuple(algorithms) if algorithms is not None else cls._supported
        if not requested:
            raise ValueError("algorithms must name at least one content coding")
        unknown = [name for name in requested if name not in _CODECS]
        if unknown:
            raise ValueError(f"unknown content coding(s) {unknown}; supported: {sorted(_CODECS)}")
        available = tuple(name for name in requested if _CODECS[name] is not None)
        if not available:
            missing = sorted({_CODECS_PACKAGE[name] for name in requested})
            raise ValueError(
                f"no compression package installed for {list(requested)}; "
                f"install {' or '.join(missing)}"
            )

        resolved: dict[str, int] = {}
        for name in available:
            codec = _CODECS[name]
            assert codec is not None
            resolved[name] = codec.default_level
        if compresslevel is not None:
            # The pre-existing single-level argument. It names gzip's scale, so
            # it applies to gzip; a multi-coding deployment uses `levels`.
            resolved["gzip"] = compresslevel
        if levels:
            unknown_levels = [name for name in levels if name not in _CODECS]
            if unknown_levels:
                raise ValueError(f"levels names unknown content coding(s) {unknown_levels}")
            resolved.update({k: v for k, v in levels.items() if k in resolved})
        return available, resolved

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
        coding = _negotiate(accept, self.algorithms)
        if coding is None:
            return response

        # Streaming bodies have no materialised `response.body`, so the
        # `minimum_size` gate (a buffered-only heuristic) does not apply.
        # Compress the stream lazily, chunk-by-chunk, and fall through to the
        # buffered path only for non-streamed responses.
        if response.is_streamed:
            return self._process_stream(request, response, coding)

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
        codec = _CODECS[coding]
        assert codec is not None
        level = self.levels[coding]
        body = response.body
        if len(body) < self.min_stream_chunk_offload:
            compressed = codec.compress(body, level)
        else:
            compressed = await offload(codec.compress, body, level)

        clen = len(compressed)
        if clen < len(response.body):
            response.body = compressed
            self._finalize_encoding_headers(response, coding, content_length=clen)

        return response

    def _process_stream(self, request: Request, response: Response, coding: str) -> Response:
        """Wrap a streaming response's body in a lazy gzip compressor.

        Mirrors the buffered guards (compressible type, no pre-existing
        encoding, no 206 / Content-Range) but skips real-time latency-sensitive
        streams (SSE) so events are not buffered through `compressobj`.
        """
        # SSE and other latency-sensitive streams trade wire size for
        # per-event delivery; routing them through a buffering compressor would
        # merge or delay frames.
        if response.is_event_source or response.mimetype in self.latency_sensitive_types:
            return response

        if self._skip_for_type_or_encoding(response):
            return response

        # Range responses are served whole and uncompressed (see the buffered
        # path for the RFC 9110 Sec. 14 rationale).
        if response.status_code == HTTP_206_PARTIAL_CONTENT or header_present(
            response.headers, HEADER_CONTENT_RANGE
        ):
            return response

        response._stream = self._compress_stream(response._stream, coding)
        # A streamed gzip body is chunked / `more_body`-framed; any declared
        # length describes the uncompressed representation, so finalize with no
        # Content-Length (the native chunked path relies on Transfer-Encoding).
        self._finalize_encoding_headers(response, coding, content_length=None)
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

    def _finalize_encoding_headers(
        self, response: Response, coding: str, content_length: int | None
    ) -> None:
        """Rewrite headers after the body bytes have been compressed.

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
        response.headers[HEADER_CONTENT_ENCODING] = coding
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

    async def _compress_stream(self, stream: Any, coding: str) -> AsyncIterator[bytes]:
        """Compress a chunk stream lazily, reusing one compressor across chunks.

        Each input chunk becomes its own deliverable frame, so a long-lived
        chunked stream (NDJSON, a log tail) is decodable as it arrives rather
        than stalling until EOF, while later chunks still benefit from the
        shared compression context. The codec's `finish` writes whatever
        trailer the coding requires - gzip's CRC/length, brotli's and zstd's
        end-of-stream markers.
        """
        codec = _CODECS[coding]
        assert codec is not None
        compressor = codec.stream(self.levels[coding])
        async for chunk in stream:
            # Downstream chunked / ASGI emit paths expect bytes; streams may
            # yield str (see `StreamingResponse._aiter_sync`).
            b = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
            if len(b) < self.min_stream_chunk_offload:
                out = compressor.frame(b)
            else:
                # Offload large frames to the thread pool, preserving ContextVars.
                out = await offload(compressor.frame, b)
            if out:
                yield out
        tail = compressor.finish()
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


class GZipMiddleware(CompressionMiddleware):
    """GZip compression for responses above a size threshold.

    Compression runs in the thread pool executor to avoid blocking the event loop.

    Offers gzip and nothing else, so a client asking for brotli or zstd is
    served an uncompressed body. Use
    [`CompressionMiddleware`](#veloce.CompressionMiddleware) to negotiate across
    the newer codings.

    Usage::

        app.add_middleware(GZipMiddleware(minimum_size=1024, compresslevel=6))
    """

    _supported = ("gzip",)
