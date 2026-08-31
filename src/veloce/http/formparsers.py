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

# Multipart-parsing safety limits - guard against algorithmic-complexity
# DoS from a body crafted with pathologically many or oversized parts.
DEFAULT_MAX_MULTIPART_PARTS = 1000
DEFAULT_MAX_MULTIPART_PART_SIZE = 10 * 1024 * 1024  # 10 MiB per part
# Spool threshold: in memory until this size, then rolls to a temp file.
MULTIPART_SPOOL_MAX_SIZE = 1024 * 1024  # 1 MiB

# RFC 2046 Sec. 5.1.1 boundary token grammar: 1-70 characters drawn from the
# bcharsnospace set plus space, with no trailing space. Validating the
# extracted boundary up front rejects a malformed Content-Type before it
# reaches the underlying parser, where an over-long or non-ASCII boundary
# would otherwise surface as an opaque parse error.
_BOUNDARY_BCHARS = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'()+_,-./:=? "
)
_MAX_BOUNDARY_LENGTH = 70

# RFC 7578 Sec. 5.1.2 leaves the field encoding to the part's own charset
# parameter. Only a small, well-understood set is honored; anything else is
# rejected rather than silently decoded with replacement characters, keeping
# the strict-by-default posture for the field text path.
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
    field values share this triage so a change to one policy cannot leave the
    other decoding differently within the same request.
    """
    if charset_fallback == "replace":
        return raw.decode("utf-8", errors="replace")
    if charset_fallback:
        return raw.decode(charset_fallback, errors="replace")
    raise BadRequest(message) from cause


class _MultipartCollector:
    """The parser's working state and callbacks for one multipart body.

    `python_multipart` drives parsing through six callbacks that share the
    in-flight part's state and every size/count cap the body is held to.
    Holding them as methods puts the caps in one `__init__` signature rather
    than in seven closed-over locals no callback's signature mentions, so a
    reader can see which limits a phase consults without reading the others.

    Slotted, so a typo is an `AttributeError` at the first write rather than a
    silently created attribute, and so the per-request allocation is a fixed
    object rather than a growable dict.
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
        # A file/field-specific size limit overrides the shared per-part cap
        # for that part kind; otherwise the shared cap applies to both.
        self.file_size_cap = max_file_size if max_file_size is not None else max_part_size
        self.field_size_cap = max_field_size if max_field_size is not None else max_part_size

        self.result = FormData()
        # True between a part's begin and its end. A body that stops mid-part
        # leaves it set, which is how a truncated upload is told apart from a
        # complete one - the parser itself reports no error for either.
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
        # Whether the current part is a file, resolved at the header->data
        # transition so the per-byte path can pick the right size cap.
        self.is_file = False
        self.size_cap = self.field_size_cap
        self.disposition_parsed = False
        # Parsed Content-Disposition params, cached at the header->data
        # transition so `on_part_end` reuses them instead of re-walking the
        # quoted-string-aware header a second time per part.
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
                    # Snapshot the per-part headers so the UploadFile owns its
                    # own copy, decoupled from the reused parser state.
                    headers=Headers(self.headers),
                ),
            )
            return

        raw_bytes = spool.read()
        spool.close()
        value = self._decode_field(name, raw_bytes)
        # Count field-name bytes toward the resident-memory ceiling so a
        # flood of large-named empty fields is bounded too.
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

        Annotated `NoReturn` so its call sites read as terminating, and so
        mypy narrows what follows them.
        """
        raise RequestEntityTooLarge(message)

    def _resolve_part_kind(self) -> None:
        # Headers are complete once part data begins; classify the part as a
        # file or text field so the right count/size limits apply per byte.
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
        # A part may declare its own charset (RFC 7578 Sec. 5.1.2); honor it
        # before falling back to the global UTF-8 / charset_fallback path.
        part_charset = _part_charset(self.headers.get("content-type", ""))
        if part_charset is not None:
            # A part that *declares* a charset is asserting its bytes are
            # valid in that encoding, so decode strictly: bytes that don't
            # match (invalid UTF-8 under charset=utf-8, a byte > 0x7f under
            # charset=ascii) are a client error, not text to silently
            # corrupt with U+FFFD. The global charset_fallback path below
            # still tolerates undeclared parts as before.
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
    matching what `MAX_FORM_PARTS = None` means for a urlencoded body. `max_files` and
    `max_fields`, when set, additionally cap file parts and text-field
    parts independently, so a form may allow many small fields while
    permitting only a few uploads (or vice versa).

    `max_part_size` caps each part's body size. `max_file_size` and
    `max_field_size`, when set, override it for file parts and text
    fields respectively, expressing the common "small fields, large
    files" policy. `max_field_memory`, when set, caps the cumulative
    resident bytes of all text fields (value bytes plus field-name
    bytes), a ceiling that `max_field_size` alone cannot express.

    Exceeding any limit raises `RequestEntityTooLarge` (413), so a
    maliciously structured form cannot exhaust memory or CPU even when
    its total size is within `MAX_CONTENT_LENGTH`.

    A missing `boundary` parameter, or one violating the RFC 2046
    boundary grammar, raises `BadRequest` (400) rather than silently
    yielding an empty form.

    `charset_fallback` controls how non-UTF-8 field bytes are handled
    when a part declares no `charset` of its own. The default (`None`)
    rejects them with `BadRequest` (400). Pass `"replace"` to substitute
    U+FFFD (the pre-0.1.4 behaviour) or `"latin-1"` to decode as
    ISO-8859-1 for legacy clients. A part that declares its own
    `Content-Type` charset (RFC 7578 Sec. 5.1.2) is decoded with that charset
    instead, provided it is one of `ascii`, `us-ascii`, `utf-8`, or
    `iso-8859-1`. A declared charset is decoded strictly: bytes that are
    invalid in it raise `BadRequest` (400) rather than being corrupted with
    U+FFFD, since the part asserted its own encoding.
    """
    # The boundary is a Content-Type parameter, so extract it with the same
    # quoted-string-aware walker used for the part headers below
    # (`parse_header_params`) rather than an ad-hoc `split(";")` + quote strip.
    # This keeps boundary parsing consistent with the rest of the module and
    # handles a quoted or whitespace-padded `boundary=...` (RFC 2046 / RFC 7578)
    # the same way every other Content-Type parameter is parsed.
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
            # A mid-body parse failure (truncated part, malformed delimiter)
            # leaves the form incomplete. Returning the partial result with a
            # 200 would silently drop the missing fields/files, so reject the
            # whole body with 400 — the same posture as a malformed boundary.
            raise BadRequest("multipart/form-data body is malformed") from exc
        if collector.part_open:
            # The body ended inside a part: no closing delimiter, and for the
            # underlying parser that is simply the end of input rather than an
            # error. Accepting it would return 200 with the truncated part's
            # field missing, so the caller could not tell a short upload from a
            # form that genuinely omitted it.
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
