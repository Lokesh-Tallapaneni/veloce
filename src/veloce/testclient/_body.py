"""Request body encoding for the test client — streams, multipart and bodies."""

from __future__ import annotations

import mimetypes
import secrets
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Iterable,
)
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import veloce._json as _json
from veloce._constants import (
    HEADER_CONTENT_TYPE,
    MIME_FORM_URLENCODED,
    MIME_JSON,
    MIME_MULTIPART_FORM_DATA,
    MIME_OCTET_STREAM,
)
from veloce._internal import _quote_header_value

if TYPE_CHECKING:  # pragma: no cover
    pass
from veloce.testclient._headers import _has_header, _header_pairs, _HeadersArg, _set_header

# What a `stream=` body may be. `_aiter_body_chunks` normalises either shape
# into an async iterator of bytes, encoding `str` chunks as UTF-8.
StreamBody = Iterable[bytes | str] | AsyncIterable[bytes | str]


async def _aiter_body_chunks(stream: Any) -> AsyncIterator[bytes]:
    """Normalise a user-supplied streaming body into an async iterator of bytes.

    A sync or async iterable of `bytes` / `str`, the latter UTF-8 encoded; any
    other element type raises `TypeError`. A plain `bytes` is not a stream and is
    rejected, since raw bytes go through `content=`.
    """
    if hasattr(stream, "__aiter__"):
        async for chunk in stream:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else _coerce_chunk(chunk)
    elif hasattr(stream, "__iter__") and not isinstance(stream, (bytes, bytearray)):
        for chunk in stream:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else _coerce_chunk(chunk)
    else:
        raise TypeError(
            "stream must be a sync Iterable or an AsyncIterable of bytes/str chunks, "
            f"got {type(stream).__name__}"
        )


def _coerce_chunk(chunk: Any) -> bytes:
    """Coerce a single non-str stream chunk to `bytes`, rejecting other types."""
    if isinstance(chunk, bytes):
        return chunk
    if isinstance(chunk, bytearray):
        return bytes(chunk)
    raise TypeError(f"stream chunk must be bytes or str, got {type(chunk).__name__}")


def _guess_content_type(filename: str | None, content: Any) -> str:
    """Pick a Content-Type for a multipart file part.

    The file-like's `.type` when present, else `mimetypes.guess_type`, else the
    `application/octet-stream` the multipart spec recommends for an unknown one.
    """
    explicit = getattr(content, "type", None)
    if explicit:
        return str(explicit)
    if filename:
        guess = mimetypes.guess_type(filename)[0]
        if guess:
            return guess
    return MIME_OCTET_STREAM


def _encode_multipart(
    files: dict[str, Any],
    fields: dict[str, str],
) -> tuple[bytes, str]:
    """Build a `multipart/form-data` body from files + extra form fields.

    `files` shape per RFC 7578 Sec. 4, mirroring `requests` / `httpx` - values
    can be:
    - `bytes` / `str`: raw file content, filename inferred from key.
    - file-like (anything with `.read()`): filename from `.name`, else the key.
    - `(filename, content_or_filelike)`: 2-tuple.
    - `(filename, content_or_filelike, content_type)`: 3-tuple.

    A shape with no explicit content-type has one guessed from the filename.
    `fields` are non-file parts; both appear in registration order. Returns
    `(body, content_type)`, where the content type carries the random boundary.
    """

    def _q(value: str) -> str:
        # Per RFC 7578 Sec. 4.2 / RFC 2616 Sec. 2.2 quoted-string: escape `"` and
        # `\`, and reject CR / LF, which would inject header fields into the preamble.
        if "\r" in value or "\n" in value:
            raise ValueError("multipart name / filename must not contain CR or LF")
        return _quote_header_value(value)

    boundary = "----veloce-" + secrets.token_hex(16)
    parts: list[bytes] = []
    b = boundary.encode("ascii")

    for name, value in fields.items():
        parts.append(b"--" + b + b"\r\n")
        parts.append(f'Content-Disposition: form-data; name="{_q(name)}"\r\n\r\n'.encode())
        parts.append(value.encode("utf-8") if isinstance(value, str) else value)
        parts.append(b"\r\n")

    for name, spec in files.items():
        # Match `requests` / `httpx`: bytes / str, a file-like (anything with
        # `.read()`), or a 2- or 3-tuple adding the filename and content type.
        if isinstance(spec, (bytes, str)):
            filename, content, ct = name, spec, MIME_OCTET_STREAM
        elif isinstance(spec, tuple) and len(spec) == 2:
            filename, content = spec
            ct = _guess_content_type(filename, content)
        elif isinstance(spec, tuple) and len(spec) == 3:
            filename, content, ct = spec
        elif hasattr(spec, "read"):
            # `.name` when present (an open file sets it, BytesIO does not).
            filename = getattr(spec, "name", None) or name
            content = spec
            ct = _guess_content_type(filename, content)
        else:
            raise TypeError(f"files[{name!r}] must be bytes, str, file-like, or 2/3-tuple")
        # Resolve the body bytes for any file-like content.
        read = getattr(content, "read", None)
        if read is not None:
            content = read()
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not isinstance(content, bytes):
            raise TypeError(f"files[{name!r}] content must be bytes, str, or file-like")
        parts.append(b"--" + b + b"\r\n")
        parts.append(
            (
                f'Content-Disposition: form-data; name="{_q(name)}"; '
                f'filename="{_q(filename)}"\r\n'
                f"Content-Type: {ct}\r\n\r\n"
            ).encode()
        )
        parts.append(content)
        parts.append(b"\r\n")

    parts.append(b"--" + b + b"--\r\n")
    body = b"".join(parts)
    return body, f"{MIME_MULTIPART_FORM_DATA}; boundary={boundary}"


def _prepare_body_request(
    json: Any,
    data: dict[str, str] | None,
    content: bytes | None,
    files: dict[str, Any] | None,
    headers: _HeadersArg,
    stream: StreamBody | None,
) -> tuple[list[tuple[str, str]], bytes | None]:
    """Return `(headers, body)` for a body-carrying request.

    `body` is `None` when `stream` was given, which excludes every buffered form.
    Enforced here, so the misuse is loud rather than one argument silently
    winning, and so both clients answer the same way.
    """
    if stream is not None:
        assert json is None and data is None and content is None and files is None, (
            "stream cannot be combined with json/data/content/files"
        )
        return _header_pairs(headers), None
    body, hdrs = _assemble_body(json, data, content, files, headers)
    return hdrs, body


def _assemble_body(
    json: Any,
    data: dict[str, str] | None,
    content: bytes | None,
    files: dict[str, Any] | None,
    headers: _HeadersArg,
) -> tuple[bytes, list[tuple[str, str]]]:
    """Encode one buffered request body and the content type it implies.

    Shared by both clients so the sync and async paths cannot drift on which
    argument wins or on the media type each form sets.
    """
    hdrs = _header_pairs(headers)
    body = b""
    if files is not None:
        body, ct = _encode_multipart(files, data or {})
        _set_header(hdrs, HEADER_CONTENT_TYPE, ct)
    elif json is not None:
        body = _json.dumps(json)
        if not _has_header(hdrs, HEADER_CONTENT_TYPE):
            hdrs.append((HEADER_CONTENT_TYPE, MIME_JSON))
    elif data is not None:
        body = urlencode(data).encode()
        if not _has_header(hdrs, HEADER_CONTENT_TYPE):
            hdrs.append((HEADER_CONTENT_TYPE, MIME_FORM_URLENCODED))
    elif content is not None:
        body = content
    return body, hdrs
