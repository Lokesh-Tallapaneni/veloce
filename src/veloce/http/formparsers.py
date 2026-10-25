"""Multipart form-data parser — RFC 2046 boundary-delimited bodies."""

from __future__ import annotations

import contextlib
import tempfile
from typing import TYPE_CHECKING, Any, NoReturn

from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError

from veloce._constants import MIME_TEXT_PLAIN
from veloce._header_parsing import parse_header_params
from veloce.exceptions import BadRequest, RequestEntityTooLarge
from veloce.http.datastructures import FormData, Headers, UploadFile

if TYPE_CHECKING:  # pragma: no cover
    from python_multipart.multipart import MultipartCallbacks

# Safety limits against a body with pathologically many or oversized parts.
DEFAULT_MAX_MULTIPART_PARTS = 1000
DEFAULT_MAX_MULTIPART_PART_SIZE = 10 * 1024 * 1024  # 10 MiB per part
# Spool threshold: in memory until this size, then rolls to a temp file.
MULTIPART_SPOOL_MAX_SIZE = 1024 * 1024  # 1 MiB

# RFC 2046 Sec. 5.1.1: 1-70 bcharsnospace characters plus space, none trailing.
# Checked up front, where the parser would report only an opaque failure.
_BOUNDARY_BCHARS = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'()+_,-./:=? "
)
_MAX_BOUNDARY_LENGTH = 70

# RFC 7578 Sec. 5.1.2 leaves the field encoding to the part's own charset;
# anything outside this set is rejected rather than decoded with U+FFFD.
_ALLOWED_PART_CHARSETS = frozenset({"ascii", "us-ascii", "utf-8", "iso-8859-1"})


def _valid_boundary(boundary: str) -> bool:
    """Return True when `boundary` satisfies the RFC 2046 bdtext grammar."""
    if not 0 < len(boundary) <= _MAX_BOUNDARY_LENGTH:
        return False
    if boundary[-1] == " ":
        return False
    return all(ch in _BOUNDARY_BCHARS for ch in boundary)


def _parse_content_disposition(value: str) -> tuple[str, dict[str, str]]:
    """Parse a Content-Disposition header per RFC 2183 / RFC 6266.

    Quoted-string aware so a literal `;` inside `name="a;b"` does not
    terminate the parameter. Backslash escapes `\\"` and `\\\\` inside
    quoted strings are unescaped per RFC 5322 quoted-pair semantics.
    """
    return parse_header_params(value, delimiter=";", unescape=True)


def _part_charset(content_type: str) -> str | None:
    """Return the validated lowercased charset declared on a part, or None.

    Reads the `charset` parameter from the part's own Content-Type header
    (RFC 7578 Sec. 5.1.2). An unrecognized charset raises `BadRequest` so the
    caller never decodes field bytes with an unsupported codec.
    """
    if not content_type:
        return None
    _, params = parse_header_params(content_type, delimiter=";", unescape=True)
    charset = params.get("charset")
    if charset is None:
        return None
    charset = charset.lower()
    if charset not in _ALLOWED_PART_CHARSETS:
        raise BadRequest(f"unsupported multipart part charset {charset!r}")
    return charset


def _decode_with_fallback(
    raw: bytes,
    charset_fallback: str | None,
    message: str,
    cause: UnicodeDecodeError,
) -> str:
    """Decode bytes that UTF-8 rejected, applying the configured fallback.

    `"replace"` substitutes U+FFFD, any other codec name decodes with
    replacement, and `None` refuses the body with `message`. Part headers and
    field values share this triage.
    """
    if charset_fallback == "replace":
        return raw.decode("utf-8", errors="replace")
    if charset_fallback:
        return raw.decode(charset_fallback, errors="replace")
    raise BadRequest(message) from cause


class _MultipartCollector:
    """The parser's working state and callbacks for one multipart body.

    `python_multipart` drives parsing through six callbacks that share the
    in-flight part's state and every size/count cap the body is held to; as
    methods, those caps sit in one `__init__` signature rather than in seven
    closed-over locals no callback's signature mentions.

    Slotted, so a typo is an `AttributeError` at the first write and the
    per-request allocation is fixed rather than a growable dict.
    """

    __slots__ = (
        "charset_fallback",
        "disposition_params",
        "disposition_parsed",
        "field_memory",
        "field_size_cap",
        "fields_seen",
        "file_size_cap",
        "files_seen",
        "header_field",
        "header_value",
        "headers",
        "is_file",
        "max_field_memory",
        "max_fields",
        "max_files",
        "max_parts",
        "part_open",
        "part_size",
        "parts_seen",
        "result",
        "size_cap",
        "spool",
    )

    def __init__(
        self,
        *,
        max_parts: int | None,
        max_files: int | None,
        max_fields: int | None,
        max_part_size: int,
        max_file_size: int | None,
        max_field_size: int | None,
        max_field_memory: int | None,
        charset_fallback: str | None,
    ) -> None:
        self.max_parts = max_parts
        self.max_files = max_files
        self.max_fields = max_fields
        self.max_field_memory = max_field_memory
        self.charset_fallback = charset_fallback
        # A per-kind limit overrides the shared cap for that kind only.
        self.file_size_cap = max_file_size if max_file_size is not None else max_part_size
        self.field_size_cap = max_field_size if max_field_size is not None else max_part_size

        self.result = FormData()
        # Set between a part's begin and end, since the parser reports no error
        # for a body that stops mid-part.
        self.part_open = False
        self.parts_seen = 0
        self.files_seen = 0
        self.fields_seen = 0
        self.field_memory = 0
        self.header_field = bytearray()
        self.header_value = bytearray()
        self.headers: dict[str, str] = {}
        self.spool: Any = None
        self.part_size = 0
        # Resolved at the header->data transition, for the per-byte size cap.
        self.is_file = False
        self.size_cap = self.field_size_cap
        self.disposition_parsed = False
        # Cached there too, so `on_part_end` does not re-walk the header.
        self.disposition_params: dict[str, str] = {}

    @property
    def callbacks(self) -> MultipartCallbacks:
        """Return the callback table `MultipartParser` is constructed with."""
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
        }

    def on_part_begin(self) -> None:
        self.part_open = True
        self.parts_seen += 1
        max_parts = self.max_parts
        if max_parts is not None and self.parts_seen > max_parts:
            self._too_large(f"multipart form exceeds the {max_parts}-part limit")
        self.headers = {}
        self.header_field = bytearray()
        self.header_value = bytearray()
        self.spool = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=MULTIPART_SPOOL_MAX_SIZE
        )
        self.part_size = 0
        self.is_file = False
        self.size_cap = self.field_size_cap
        self.disposition_parsed = False
        self.disposition_params = {}

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self.header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self.header_value += data[start:end]

    def on_header_end(self) -> None:
        raw_name = bytes(self.header_field)
        raw_value = bytes(self.header_value)
        try:
            name = raw_name.decode("utf-8").lower()
            value = raw_value.decode("utf-8")
        except UnicodeDecodeError as exc:
            fallback = self.charset_fallback
            message = "multipart part header is not valid UTF-8"
            name = _decode_with_fallback(raw_name, fallback, message, exc).lower()
            value = _decode_with_fallback(raw_value, fallback, message, exc)
        self.headers[name] = value
        self.header_field = bytearray()
        self.header_value = bytearray()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if not self.disposition_parsed:
            self._resolve_part_kind()
        chunk_len = end - start
        self.part_size += chunk_len
        cap = self.size_cap
        if self.part_size > cap:
            kind = "file" if self.is_file else "field"
            self._too_large(f"multipart {kind} exceeds the {cap}-byte size limit")
        max_field_memory = self.max_field_memory
        if not self.is_file and max_field_memory is not None:
            self.field_memory += chunk_len
            if self.field_memory > max_field_memory:
                self._too_large(
                    f"multipart text fields exceed the {max_field_memory}-byte memory limit"
                )
        self.spool.write(data[start:end])

    def on_part_end(self) -> None:
        self.part_open = False
        # An empty part never emits part data, so classify it here too.
        if not self.disposition_parsed:
            self._resolve_part_kind()
        # Reuse the Content-Disposition params parsed in _resolve_part_kind.
        params = self.disposition_params
        name = params.get("name", "")
        filename = params.get("filename")

        spool = self.spool
        self.spool = None
        if not name:
            spool.close()
            return

        spool.seek(0)
        if filename is not None:
            self.result.add(
                name,
                UploadFile(
                    filename=filename,
                    content_type=self.headers.get("content-type", MIME_TEXT_PLAIN),
                    file=spool,
                    size=self.part_size,
                    # Snapshotted, so the UploadFile does not share parser state.
                    headers=Headers(self.headers),
                ),
            )
            return

        raw_bytes = spool.read()
        spool.close()
        value = self._decode_field(name, raw_bytes)
        # Field names count too, so large-named empty fields stay bounded.
        max_field_memory = self.max_field_memory
        if max_field_memory is not None:
            self.field_memory += len(name.encode("utf-8"))
            if self.field_memory > max_field_memory:
                self._too_large(
                    f"multipart text fields exceed the {max_field_memory}-byte memory limit"
                )
        self.result.add(name, value)

    def _too_large(self, message: str) -> NoReturn:
        """Raise `RequestEntityTooLarge`; never returns.

        Annotated `NoReturn` so its call sites read as terminating and mypy
        narrows what follows them.
        """
        raise RequestEntityTooLarge(message)

    def _resolve_part_kind(self) -> None:
        # Headers are complete once data begins, so the per-byte caps resolve here.
        disposition = self.headers.get("content-disposition", "")
        _, params = _parse_content_disposition(disposition)
        self.disposition_params = params
        is_file = params.get("filename") is not None
        self.is_file = is_file
        self.size_cap = self.file_size_cap if is_file else self.field_size_cap
        if is_file:
            self.files_seen += 1
            max_files = self.max_files
            if max_files is not None and self.files_seen > max_files:
                self._too_large(f"multipart form exceeds the {max_files}-file limit")
        else:
            self.fields_seen += 1
            max_fields = self.max_fields
            if max_fields is not None and self.fields_seen > max_fields:
                self._too_large(f"multipart form exceeds the {max_fields}-field limit")
        self.disposition_parsed = True

    def _decode_field(self, name: str, raw_bytes: bytes) -> str:
        # A part's own charset (RFC 7578 Sec. 5.1.2) wins over the global path.
        part_charset = _part_charset(self.headers.get("content-type", ""))
        if part_charset is not None:
            # A declared charset asserts validity, so a mismatch is a client error.
            try:
                return raw_bytes.decode(part_charset)
            except UnicodeDecodeError as exc:
                raise BadRequest(
                    f"multipart field {name!r} value is not valid {part_charset}"
                ) from exc
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            return _decode_with_fallback(
                raw_bytes,
                self.charset_fallback,
                f"multipart field {name!r} value is not valid UTF-8",
                exc,
            )


def parse_multipart_form(
    body: bytes,
    content_type: str,
    *,
    max_parts: int | None = DEFAULT_MAX_MULTIPART_PARTS,
    max_files: int | None = None,
    max_fields: int | None = None,
    max_part_size: int = DEFAULT_MAX_MULTIPART_PART_SIZE,
    max_file_size: int | None = None,
    max_field_size: int | None = None,
    max_field_memory: int | None = None,
    charset_fallback: str | None = None,
) -> FormData:
    """Parse multipart/form-data into FormData with UploadFile support.

    `max_parts` caps the total number of parts; `None` disables the cap,
    matching what `MAX_FORM_PARTS = None` means for a urlencoded body.
    `max_files` and `max_fields` cap file and text-field parts independently.

    `max_part_size` caps each part's body size, and `max_file_size` /
    `max_field_size` override it per part kind. `max_field_memory` caps the
    cumulative resident bytes of all text fields, value plus field-name bytes -
    a ceiling `max_field_size` alone cannot express.

    Exceeding any limit raises `RequestEntityTooLarge` (413), so a maliciously
    structured form cannot exhaust memory or CPU within `MAX_CONTENT_LENGTH`.
    A missing `boundary`, or one violating the RFC 2046 grammar, raises
    `BadRequest` (400) rather than silently yielding an empty form.

    `charset_fallback` handles non-UTF-8 field bytes where a part declares no
    `charset`: the default `None` rejects them with `BadRequest` (400), while
    `"replace"` substitutes U+FFFD and `"latin-1"` decodes as ISO-8859-1. A part
    declaring its own charset (RFC 7578 Sec. 5.1.2, one of `ascii`, `us-ascii`,
    `utf-8`, `iso-8859-1`) is decoded strictly with it, so invalid bytes raise
    `BadRequest` rather than being corrupted.
    """
    # The same walker as the part headers, so a quoted `boundary=` parses alike.
    _, ct_params = parse_header_params(content_type, delimiter=";", unescape=True)
    boundary_present = "boundary" in ct_params
    boundary = ct_params.get("boundary", "")

    if not boundary_present:
        raise BadRequest("multipart/form-data is missing the boundary parameter")
    if not _valid_boundary(boundary):
        raise BadRequest("multipart/form-data boundary is malformed")

    collector = _MultipartCollector(
        max_parts=max_parts,
        max_files=max_files,
        max_fields=max_fields,
        max_part_size=max_part_size,
        max_file_size=max_file_size,
        max_field_size=max_field_size,
        max_field_memory=max_field_memory,
        charset_fallback=charset_fallback,
    )
    result = collector.result
    parser = MultipartParser(boundary.encode("latin-1"), collector.callbacks)
    try:
        try:
            parser.write(body)
            parser.finalize()
        except FormParserError as exc:
            # A 200 with the partial form would silently drop the missing fields.
            raise BadRequest("multipart/form-data body is malformed") from exc
        if collector.part_open:
            # The parser reads a missing closing delimiter as the end of input.
            raise BadRequest("multipart/form-data body is truncated")
    except BaseException:
        for value in result.values():
            if isinstance(value, UploadFile):
                with contextlib.suppress(Exception):
                    value.file.close()
        raise
    finally:
        in_progress = collector.spool
        if in_progress is not None:
            with contextlib.suppress(Exception):
                in_progress.close()
    return result
