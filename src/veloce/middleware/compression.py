"""Response compression middleware — CPU-bound work offloaded to executor."""

from __future__ import annotations

import gzip
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

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

# Image/video/audio/zip are absent: they carry their own compression already.
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
    `finish() -> bytes`. `frame` must flush what the codec is holding - or a
    long-lived chunked stream stalls until EOF - without ending the context.
    """

    name: str
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
        return self._co.flush(zlib.Z_FINISH)


def _gzip_codec() -> _Codec:
    return _Codec("gzip", 6, lambda data, level: gzip.compress(data, level), _ZlibStream)


def _brotli_codec() -> _Codec | None:
    try:
        import brotli
    except ImportError:
        try:
            import brotlicffi as brotli  # pyright: ignore[reportMissingImports]
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

    # Quality 4, not 11: brotli's top qualities cost orders of magnitude of CPU.
    return _Codec("br", 4, lambda data, level: brotli.compress(data, quality=level), _BrotliStream)


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
        3,
        lambda data, level: zstandard.ZstdCompressor(level=level).compress(data),
        _ZstdStream,
    )


# Resolved once at import, so a per-response path never pays an import attempt.
_CODECS: dict[str, _Codec] = {
    name: codec
    for name, codec in (
        ("zstd", _zstd_codec()),
        ("br", _brotli_codec()),
        ("gzip", _gzip_codec()),
    )
    if codec is not None
}

# Server preference when the caller names none: best ratio first.
_DEFAULT_ALGORITHMS = ("zstd", "br", "gzip")

# Every coding this knows, installed or not, and the package to install for it.
_CODECS_PACKAGE = {"zstd": "zstandard", "br": "brotli", "gzip": "gzip (stdlib)"}


def _quality(pieces: list[str]) -> float:
    """Read the `q` weight of one media range, defaulting to 1 (RFC 9110 Sec. 12.4.2).

    Both spellings, since RFC 5234 Sec. 2.3 makes the ABNF literal
    case-insensitive. A malformed weight falls back to the default rather than
    failing, since the header is client input.
    """
    for param in pieces[1:]:
        param = param.strip()
        if param[:2] in ("q=", "Q="):
            try:
                return float(param[2:])
            except ValueError:
                return 1.0
    return 1.0


# Distinct `Accept-Encoding` strings a deployment sees number in the dozens.
_MAX_NEGOTIATED = 256


def _negotiate(accept: str, offered: tuple[str, ...]) -> str | None:
    """Pick the content coding to use, or `None` to send the body unencoded.

    The client's weights rank the candidates and the server's `offered` order
    breaks ties. `q=0` is a refusal; `*` supplies a weight for any coding the
    header does not name.
    """
    if not accept:
        return None

    # Fast path for the shape browsers actually send - bare tokens, no weights.
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
        # Strictly greater, so `offered` order breaks a tie.
        if weight > best_weight:
            best, best_weight = coding, weight
    return best


class CompressionMiddleware(Middleware):
    """Response compression, negotiated over the codings the server can emit.

    Offers zstd, brotli and gzip - whichever of their packages are installed -
    and picks one per response from `Accept-Encoding`. The client's `q` weights
    rank the candidates; `algorithms` order breaks ties. Compression above
    `min_stream_chunk_offload` bytes runs in the thread pool.

    Usage::

        from veloce import CompressionMiddleware

        app.add_middleware(CompressionMiddleware(algorithms=("br", "gzip")))
    """

    #: Codings this class may offer. `GZipMiddleware` narrows it to gzip.
    _supported: ClassVar[tuple[str, ...]] = _DEFAULT_ALGORITHMS

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
        # A pure function of the header, since `self.algorithms` is fixed here.
        # Cleared wholesale on overflow, so junk headers cannot lock it up.
        self._negotiated: dict[str, str | None] = {}
        # Matched as a prefix, so `"text/"` covers every text/* media type.
        self.include_types: tuple[str, ...] = (
            tuple(include_types) if include_types is not None else _DEFAULT_COMPRESSIBLE
        )
        self.exclude_types: tuple[str, ...] = tuple(exclude_types)
        # One threshold for both halves: a buffered body and a streamed chunk
        # ask the same question of the pool.
        self.min_stream_chunk_offload = min_stream_chunk_offload
        # SSE trades wire size for per-event latency, so it is never buffered.
        self.latency_sensitive_types = latency_sensitive_types

    @classmethod
    def _resolve_codings(
        cls,
        algorithms: tuple[str, ...] | None,
        levels: dict[str, int] | None,
        compresslevel: int | None,
    ) -> tuple[tuple[str, ...], dict[str, int]]:
        """Validate the requested codings and settle a level for each.

        An algorithm whose package is missing is dropped rather than made fatal.
        Asking *only* for a missing one raises, naming the package to install,
        since silently serving plaintext would hide it.
        """
        requested = tuple(algorithms) if algorithms is not None else cls._supported
        if not requested:
            raise ValueError("algorithms must name at least one content coding")
        unknown = [name for name in requested if name not in _CODECS_PACKAGE]
        if unknown:
            raise ValueError(
                f"unknown content coding(s) {unknown}; supported: {sorted(_CODECS_PACKAGE)}"
            )
        available = tuple(name for name in requested if name in _CODECS)
        if not available:
            missing = sorted({_CODECS_PACKAGE[name] for name in requested})
            raise ValueError(
                f"no compression package installed for {list(requested)}; "
                f"install {' or '.join(missing)}"
            )

        resolved: dict[str, int] = {}
        for name in available:
            resolved[name] = _CODECS[name].default_level
        if compresslevel is not None:
            # It names gzip's scale; a multi-coding deployment uses `levels`.
            resolved["gzip"] = compresslevel
        if levels:
            unknown_levels = [name for name in levels if name not in _CODECS_PACKAGE]
            if unknown_levels:
                raise ValueError(f"levels names unknown content coding(s) {unknown_levels}")
            resolved.update({k: v for k, v in levels.items() if k in resolved})
        return available, resolved

    async def process_response(self, request: Request, response: Response) -> Response:
        """Compress the response body with the coding the client accepts."""
        # Every response this touches varies on Accept-Encoding, uncompressed
        # ones too, and RFC 9110 Sec. 12.5.5 makes that a cache's input.
        response.add_vary(HEADER_ACCEPT_ENCODING)
        accept = request.headers.get(HEADER_ACCEPT_ENCODING, "")
        memo = self._negotiated
        if accept in memo:
            coding = memo[accept]
        else:
            coding = _negotiate(accept, self.algorithms)
            if len(memo) >= _MAX_NEGOTIATED:
                memo.clear()
            memo[accept] = coding
        if coding is None:
            return response

        # No materialised `response.body`, so the `minimum_size` gate cannot apply.
        if response.is_streamed:
            return self._process_stream(request, response, coding)

        if len(response.body) < self.minimum_size:
            return response

        if self._skip_compression(response):
            return response

        # Measured at 32 concurrent requests, completed requests per second,
        # inline vs offloaded: 2 KiB 15795 vs 5684, 6 KiB 12489 vs 5393,
        # 32 KiB 5432 vs 4102, 48 KiB 4043 vs 4101, 128 KiB 1348 vs 2814,
        # 512 KiB 316 vs 1045. The crossover is near 48 KiB, and the threshold
        # sits below it so the offload starts before inline becomes slower.
        codec = _CODECS[coding]
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
        """Wrap a streaming response's body in a lazy compressor.

        Shares `_skip_compression` with the buffered path, and additionally
        skips latency-sensitive streams (SSE), whose frames a buffering
        compressor would merge or delay.
        """
        if response.is_event_source or response.mimetype in self.latency_sensitive_types:
            return response

        if self._skip_compression(response):
            return response

        response._stream = self._compress_stream(response._stream, coding)
        # Framed as chunks, and a declared length describes the uncompressed body.
        self._finalize_encoding_headers(response, coding, content_length=None)
        return response

    def _skip_compression(self, response: Response) -> bool:
        """True when the response must not be compressed, on range, type or encoding grounds.

        A 206, or any response carrying Content-Range, is served uncompressed:
        compressing changes the bytes while Content-Range / Accept-Ranges / ETag
        keep describing the uncompressed representation (RFC 9110 Sec. 14). A
        non-identity Content-Encoding is passed through untouched, since
        RFC 9110 Sec. 8.4 gives each one transformation. Field names are
        case-insensitive (RFC 9110 Sec. 5.1), so any casing is honored.
        """
        if response.status_code == HTTP_206_PARTIAL_CONTENT or header_present(
            response.headers, HEADER_CONTENT_RANGE
        ):
            return True
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

        Field names are case-insensitive (RFC 9110 Sec. 5.1), so every existing
        spelling is dropped first - a stale mixed-case length would describe the
        uncompressed body. `content_length` is `None` on the streaming path,
        whose framing carries no declared length.
        """
        self._drop_header(response, HEADER_CONTENT_ENCODING)
        self._drop_header(response, HEADER_CONTENT_LENGTH)
        response.headers[HEADER_CONTENT_ENCODING] = coding
        if content_length is not None:
            response.headers[HEADER_CONTENT_LENGTH] = str(content_length)
        # `Vary: Accept-Encoding` is set by `process_response` at entry.
        response._encoded = None
        self._weaken_strong_etag(response)

    @staticmethod
    def _drop_header(response: Response, name: str) -> None:
        """Remove every casing of `name` from the response headers.

        Field names are case-insensitive (RFC 9110 Sec. 5.1), so the loop runs
        until none remain and accidental duplicates go too.
        """
        while (key := header_key(response.headers, name)) is not None:
            del response.headers[key]

    async def _compress_stream(self, stream: Any, coding: str) -> AsyncIterator[bytes]:
        """Compress a chunk stream lazily, reusing one compressor across chunks.

        Each input chunk becomes its own deliverable frame, so a long-lived
        stream is decodable as it arrives rather than stalling until EOF, while
        later chunks still benefit from the shared compression context.
        """
        codec = _CODECS[coding]
        compressor = codec.stream(self.levels[coding])
        async for chunk in stream:
            # Downstream emit paths expect bytes; streams may yield str.
            b = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
            if len(b) < self.min_stream_chunk_offload:
                out = compressor.frame(b)
            else:
                out = await offload(compressor.frame, b)
            if out:
                yield out
        tail = compressor.finish()
        if tail:
            yield tail

    def _weaken_strong_etag(self, response: Response) -> None:
        """Downgrade a strong ETag to weak after the wire bytes change.

        A strong ETag means byte-identical representations (RFC 9110
        Sec. 8.8.1), which compression breaks. Already-weak or malformed
        (non-quoted) tags are left untouched, so no validator is fabricated.
        """
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

    Offers gzip and nothing else, so a client asking for brotli or zstd is
    served an uncompressed body. Use
    [`CompressionMiddleware`](#veloce.CompressionMiddleware) to negotiate across
    the newer codings.

    Usage::

        app.add_middleware(GZipMiddleware(minimum_size=1024, compresslevel=6))
    """

    _supported = ("gzip",)
