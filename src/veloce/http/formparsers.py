"""Multipart form-data parser — RFC 2046 boundary-delimited bodies."""

from __future__ import annotations

import contextlib
import logging
import tempfile
from typing import Any

from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError

from veloce._constants import MIME_TEXT_PLAIN
from veloce._header_parsing import parse_header_params
from veloce.exceptions import BadRequest, RequestEntityTooLarge
from veloce.http.datastructures import FormData, UploadFile

_logger = logging.getLogger(__name__)

# Multipart-parsing safety limits — guard against algorithmic-complexity
# DoS from a body crafted with pathologically many or oversized parts.
DEFAULT_MAX_MULTIPART_PARTS = 1000
DEFAULT_MAX_MULTIPART_PART_SIZE = 10 * 1024 * 1024  # 10 MiB per part
# Spool threshold: in memory until this size, then rolls to a temp file.
MULTIPART_SPOOL_MAX_SIZE = 1024 * 1024  # 1 MiB


def _parse_content_disposition(value: str) -> tuple[str, dict[str, str]]:
    """Parse a Content-Disposition header per RFC 2183 / RFC 6266.

    Quoted-string aware so a literal `;` inside `name="a;b"` does not
    terminate the parameter. Backslash escapes `\\"` and `\\\\` inside
    quoted strings are unescaped per RFC 5322 quoted-pair semantics.
    """
    return parse_header_params(value, delimiter=";", unescape=True)


def parse_multipart_form(
    body: bytes,
    content_type: str,
    *,
    max_parts: int = DEFAULT_MAX_MULTIPART_PARTS,
    max_part_size: int = DEFAULT_MAX_MULTIPART_PART_SIZE,
    charset_fallback: str | None = None,
) -> FormData:
    """Parse multipart/form-data into FormData with UploadFile support.

    `max_parts` caps how many parts the form may contain and
    `max_part_size` caps each part's body size. Exceeding either raises
    `RequestEntityTooLarge` (413), so a maliciously structured form
    cannot exhaust memory or CPU even when its total size is within
    `MAX_CONTENT_LENGTH`.

    `charset_fallback` controls how non-UTF-8 bytes are handled. The
    default (`None`) rejects them with `BadRequest` (400). Pass
    `"replace"` to substitute U+FFFD (the pre-0.1.4 behaviour) or
    `"latin-1"` to decode as ISO-8859-1 for legacy clients.
    """
    boundary = ""
    for seg in content_type.split(";"):
        seg = seg.strip()
        if seg.startswith("boundary="):
            boundary = seg[9:].strip('"')
            break

    if not boundary:
        return FormData()

    result = FormData()
    state: dict[str, Any] = {
        "parts_seen": 0,
        "header_field": bytearray(),
        "header_value": bytearray(),
        "headers": {},
        "spool": None,
        "part_size": 0,
    }

    def _too_large(message: str) -> None:
        raise RequestEntityTooLarge(message)

    def on_part_begin() -> None:
        state["parts_seen"] += 1
        if state["parts_seen"] > max_parts:
            _too_large(f"multipart form exceeds the {max_parts}-part limit")
        state["headers"] = {}
        state["header_field"] = bytearray()
        state["header_value"] = bytearray()
        state["spool"] = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=MULTIPART_SPOOL_MAX_SIZE
        )
        state["part_size"] = 0

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state["header_field"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state["header_value"] += data[start:end]

    def on_header_end() -> None:
        raw_name = bytes(state["header_field"])
        raw_value = bytes(state["header_value"])
        try:
            name = raw_name.decode("utf-8").lower()
            value = raw_value.decode("utf-8")
        except UnicodeDecodeError as exc:
            if charset_fallback == "replace":
                name = raw_name.decode("utf-8", errors="replace").lower()
                value = raw_value.decode("utf-8", errors="replace")
            elif charset_fallback:
                name = raw_name.decode(charset_fallback, errors="replace").lower()
                value = raw_value.decode(charset_fallback, errors="replace")
            else:
                raise BadRequest("multipart part header is not valid UTF-8") from exc
        state["headers"][name] = value
        state["header_field"] = bytearray()
        state["header_value"] = bytearray()

    def on_part_data(data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        state["part_size"] += len(chunk)
        if state["part_size"] > max_part_size:
            _too_large(f"multipart part exceeds the {max_part_size}-byte size limit")
        state["spool"].write(chunk)

    def on_part_end() -> None:
        disposition = state["headers"].get("content-disposition", "")
        _, params = _parse_content_disposition(disposition)
        name = params.get("name", "")
        filename = params.get("filename")

        spool = state["spool"]
        state["spool"] = None
        if not name:
            spool.close()
            return

        spool.seek(0)
        if filename is not None:
            result.add(
                name,
                UploadFile(
                    filename=filename,
                    content_type=state["headers"].get("content-type", MIME_TEXT_PLAIN),
                    file=spool,
                    size=state["part_size"],
                ),
            )
        else:
            raw_bytes = spool.read()
            try:
                value = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                if charset_fallback == "replace":
                    value = raw_bytes.decode("utf-8", errors="replace")
                elif charset_fallback:
                    value = raw_bytes.decode(charset_fallback, errors="replace")
                else:
                    spool.close()
                    raise BadRequest(f"multipart field {name!r} value is not valid UTF-8") from exc
            spool.close()
            result.add(name, value)

    parser = MultipartParser(
        boundary.encode("latin-1"),
        {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
        },
    )
    try:
        try:
            parser.write(body)
            parser.finalize()
        except FormParserError as exc:
            _logger.warning("Multipart form parse error (partial data returned): %s", exc)
    except BaseException:
        for value in result.values():
            if isinstance(value, UploadFile):
                with contextlib.suppress(Exception):
                    value.file.close()
        raise
    finally:
        in_progress = state["spool"]
        if in_progress is not None:
            with contextlib.suppress(Exception):
                in_progress.close()
    return result
