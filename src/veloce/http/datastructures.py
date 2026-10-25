"""Core data structures — the request and response containers.

`Headers` and `QueryParams` subclass `multidict.CIMultiDict` and
`multidict.MultiDict` respectively. They preserve duplicate keys and add
the `getlist` alias on top of multidict's native `getall`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import io
import ipaddress
import math
from collections.abc import Iterator, Mapping
from typing import Any, BinaryIO, NamedTuple
from urllib.parse import parse_qsl

from multidict import CIMultiDict, MultiDict

from veloce._constants import (
    HEADER_HOST,
    HEADER_X_FORWARDED_PROTO,
    MIME_JSON,
    MIME_OCTET_STREAM,
)
from veloce._header_parsing import parse_header_params, parse_media_type_params
from veloce._internal import _decode_basic_credentials, _quote_header_value, is_default_port
from veloce._protocol_constants import URL_SCHEME_HTTP, URL_SCHEME_HTTPS
from veloce.exceptions import FilesKeyError, RequestURITooLong
from veloce.http.cookies import iter_cookies

# Cap on number of query-string fields parsed per request to bound CPU
# and memory under hash-collision / parameter-pollution DoS.
_MAX_QUERY_FIELDS = 1000


def _parse_qs_pairs(value: str, max_fields: int | None) -> list[tuple[str, str]]:
    """Parse `a=1&b=2` into ordered pairs, decoding escapes only when present.

    `parse_qsl` is pure Python and calls `unquote_plus` twice per field, which
    allocates before scanning for `%`; a value carrying neither `%` nor `+` -
    the shape of most query strings - has nothing for that to do. Escaped input
    delegates to `parse_qsl`, which stays the single decoder.

    `parse_qsl` counts fields as `1 + value.count("&")`, exactly
    `len(value.split("&"))`, so `max_fields` binds at the same boundary either
    way.
    """
    if "%" in value or "+" in value:
        return parse_qsl(value, keep_blank_values=True, max_num_fields=max_fields)
    parts = value.split("&")
    if max_fields is not None and len(parts) > max_fields:
        raise ValueError("Max number of fields exceeded")
    # Matching `parse_qsl(keep_blank_values=True)`: `a=` is `("a", "")`.
    return [(k, v) for k, _, v in (part.partition("=") for part in parts if part)]


# ── Request scope primitives ──────────────────────────────
class Address(NamedTuple):
    """Client/server address - ASGI shape.

    A two-field named tuple so `request.client.host` /
    `request.client.port` work, while `host, port = request.client`
    unpacking also works (tuple semantics).
    """

    host: str
    port: int


class State(dict[str, Any]):
    """Per-request scratch namespace - supports both styles.

    A `dict` subclass whose attribute access maps to items, so
    `request.state.user` and `state["user"]` / `state.get("user")` are
    interchangeable. The dispatcher also stashes framework internals
    (`session`, `url_rule`, ...) here by key.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError:
            raise AttributeError(name) from None


# ── Uploaded files ────────────────────────────────────────
class UploadFile:
    """Uploaded file with an async read/write interface."""

    __slots__ = ("filename", "content_type", "file", "size", "headers")

    def __init__(
        self,
        filename: str,
        content_type: str = MIME_OCTET_STREAM,
        file: BinaryIO | None = None,
        size: int = 0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.filename = filename
        self.content_type = content_type
        self.file = file or io.BytesIO()
        self.size = size
        # A case-insensitive view, so a handler reads
        # `upload.headers["Content-Transfer-Encoding"]` off lowercased keys.
        self.headers = headers if isinstance(headers, Headers) else Headers(headers or {})

    @property
    def content(self) -> bytes:
        """Return the full file content as bytes.

        Synchronous: for an upload spooled to disk the underlying ``read()``
        blocks, so prefer ``await read()`` in an async context.
        """
        pos = self.file.tell()
        self.file.seek(0)
        data = self.file.read()
        self.file.seek(pos)
        return data

    def save(self, destination: str | BinaryIO, buffer_size: int = 16384) -> None:
        """Stream this upload into `destination`.

        `destination` is a filesystem path (`str`), opened `"wb"` and closed
        afterwards, or an already-open binary file the caller still owns.
        `buffer_size` bounds memory while streaming. The read cursor is reset to
        0 and restored afterwards, so the upload stays available.

        This opens and writes on the calling thread; in an async handler use
        `save_async`, which is the same work hopped to a thread.
        """
        pos = self.file.tell()
        try:
            self.file.seek(0)
            if isinstance(destination, str):
                with open(destination, "wb") as out:
                    self._stream_into(out, buffer_size)
            else:
                self._stream_into(destination, buffer_size)
        finally:
            # A closed or one-shot stream rejects the re-seek; we are done.
            with contextlib.suppress(ValueError, OSError):
                self.file.seek(pos)

    # ── Async file API ─────────────────────────────────────

    async def save_async(self, destination: str | BinaryIO, buffer_size: int = 16384) -> None:
        """Stream this upload into `destination` without blocking the event loop.

        The async counterpart of `save`, with identical behaviour.
        """
        await asyncio.to_thread(self.save, destination, buffer_size)

    async def read(self, size: int = -1) -> bytes:
        """Read up to size bytes from the upload."""
        # In-memory objects stay on the loop; a rolled spool hops to a thread.
        if self._file_is_in_memory():
            return self.file.read(size)
        return await asyncio.to_thread(self.file.read, size)

    async def write(self, data: bytes) -> int:
        """Write data to the upload's spool file."""
        if self._file_is_in_memory():
            return self.file.write(data)
        return await asyncio.to_thread(self.file.write, data)

    async def seek(self, offset: int) -> None:
        """Seek to a position in the upload's spool file."""
        if self._file_is_in_memory():
            self.file.seek(offset)
            return
        await asyncio.to_thread(self.file.seek, offset)

    async def close(self) -> None:
        """Close the upload's underlying spool file."""
        if self._file_is_in_memory():
            self.file.close()
            return
        await asyncio.to_thread(self.file.close)

    # ── Internals and context management ───────────────────

    def _file_is_in_memory(self) -> bool:
        """`True` when reads/writes are pure-Python memory ops.

        True for a `BytesIO` and for a `SpooledTemporaryFile` that has not
        rolled over; once it rolls over, every op is a syscall and must go to a
        thread. `_rolled` is a stdlib `SpooledTemporaryFile` attribute, and
        anything else is treated as on-disk.
        """
        if isinstance(self.file, io.BytesIO):
            return True
        return getattr(self.file, "_rolled", None) is False

    def _stream_into(self, out: BinaryIO, buffer_size: int) -> None:
        while True:
            chunk = self.file.read(buffer_size)
            if not chunk:
                break
            out.write(chunk)

    async def __aenter__(self) -> UploadFile:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"UploadFile(filename={self.filename!r}, content_type={self.content_type!r}, size={self.size})"


# ── URL ───────────────────────────────────────────────────
# Matches `URL.__init__`, so a rejected Host never leaks its own string.
_DEFAULT_HOST = "localhost"

# RFC 3986 Sec. 3.2.2 reg-name, plus `%` for percent-encoded octets. Anything
# else is illegal in the authority's host and is exactly what a Host-injection
# payload (`evil.com/path?x`, `a\r\nX: y`) carries.
_HOST_REGNAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "-._~"  # unreserved
    "!$&'()*+,;="  # sub-delims
    "%"  # pct-encoded marker
)


def _is_valid_reg_name(host: str) -> bool:
    """Whether `host` is a non-empty RFC 3986 reg-name (no port, no brackets)."""
    return bool(host) and all(c in _HOST_REGNAME_CHARS for c in host)


def _is_valid_ipv6(host: str) -> bool:
    """Whether `host` is a syntactically valid IPv6 literal (brackets stripped).

    Parsed with `ipaddress.IPv6Address`, so `:::1` and `2001:::1` are rejected
    where a bare character-class check would accept them.
    """
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return False
    return True


def _parse_host_header(host_header: str) -> tuple[str, int | None]:
    """Split a Host header into a validated `(host, port)`.

    A Host whose host component is not a valid RFC 3986 reg-name or IPv6
    literal, or whose port is not 1-65535, falls back to `(_DEFAULT_HOST, None)`
    rather than flowing into absolute-URL construction, where it could inject a
    path/query/CRLF or an attacker-chosen origin.
    """
    if "[" in host_header:
        # Bracketed IPv6: [::1]:8080
        bracket_start = host_header.index("[")
        bracket_end = host_header.find("]")
        # A `[` anywhere but the start, or a missing `]`, is malformed.
        if bracket_start != 0 or bracket_end == -1:
            return (_DEFAULT_HOST, None)
        host = host_header[1:bracket_end]
        if not _is_valid_ipv6(host):
            return (_DEFAULT_HOST, None)
        rest = host_header[bracket_end + 1 :]
        if not rest:
            return (host, None)
        if rest[0] != ":":
            # Trailing garbage after `]` that is not a port separator.
            return (_DEFAULT_HOST, None)
        port = _parse_port(rest[1:])
        if port is None:
            return (_DEFAULT_HOST, None)
        return (host, port)
    if host_header.count(":") >= 2:
        # Bare IPv6 (no brackets, no port): 2001:db8::1
        if not _is_valid_ipv6(host_header):
            return (_DEFAULT_HOST, None)
        return (host_header, None)
    if ":" in host_header:
        host, _, port_str = host_header.rpartition(":")
        if not _is_valid_reg_name(host):
            return (_DEFAULT_HOST, None)
        port = _parse_port(port_str)
        if port is None:
            return (_DEFAULT_HOST, None)
        return (host, port)
    if not _is_valid_reg_name(host_header):
        return (_DEFAULT_HOST, None)
    return (host_header, None)


def _parse_port(port_str: str) -> int | None:
    """Parse a Host-header port. Returns None for empty / non-1-65535 values."""
    if not port_str.isdigit():
        return None
    port = int(port_str)
    if 1 <= port <= 65535:
        return port
    return None


class URL:
    """Parsed URL with component access - lazily constructed."""

    __slots__ = (
        "scheme",
        "host",
        "port",
        "path",
        "query_string",
        "fragment",
        "_full",
    )

    def __init__(
        self,
        scheme: str = URL_SCHEME_HTTP,
        host: str = "localhost",
        port: int | None = None,
        path: str = "/",
        query_string: str = "",
        fragment: str = "",
    ) -> None:
        self.scheme = scheme
        self.host = host
        self.port = port
        self.path = path
        self.query_string = query_string
        self.fragment = fragment
        self._full: str | None = None

    @classmethod
    def from_request(
        cls,
        headers: Mapping[str, str],
        path: str,
        query_string: str,
        scope_scheme: str | None = None,
        forwarded_port: int | None = None,
        trust_forwarded_proto: bool = True,
    ) -> URL:
        """Construct a URL from request headers and path components.

        `trust_forwarded_proto` is False once `ProxyFix` has run, which writes
        the scheme it trusted into the scope; reading the raw header afterwards
        would let a hop it refused set the scheme anyway. With no `ProxyFix`
        installed the header remains a convenience default.

        `forwarded_port` is the public port a trusted reverse proxy supplied. A
        port embedded in the Host header always wins, so `forwarded_port` only
        fills one in when the Host header carries none.
        """
        host_header = headers.get(HEADER_HOST, "localhost")
        # Precedence: the ASGI scope's `scheme`, then a trusted
        # `X-Forwarded-Proto`, then plain `http`. Lower-cased once, because
        # RFC 3986 Sec. 3.1 and RFC 7239 Sec. 4 make it case-insensitive.
        if scope_scheme:
            scheme = scope_scheme.lower()
        elif (
            trust_forwarded_proto
            and headers.get(HEADER_X_FORWARDED_PROTO, "").lower() == URL_SCHEME_HTTPS
        ):
            scheme = URL_SCHEME_HTTPS
        else:
            scheme = URL_SCHEME_HTTP
        # Validated against the RFC 3986 Sec. 3.2.2 host grammar before it
        # reaches host / netloc / base_url / url_root.
        host, port = _parse_host_header(host_header)
        # The Host header is authoritative for the port, so a stale forwarded
        # one cannot override an explicit `host:port`.
        if port is None and forwarded_port is not None:
            port = forwarded_port
        return cls(
            scheme=scheme,
            host=host,
            port=port,
            path=path,
            query_string=query_string,
        )

    @property
    def netloc(self) -> str:
        """Return the network location (host:port)."""
        host_str = f"[{self.host}]" if ":" in self.host else self.host
        if self.port and not is_default_port(self.scheme, self.port):
            return f"{host_str}:{self.port}"
        return host_str

    def replace(self, **kwargs: Any) -> URL:
        """Return a new URL with the specified components replaced."""
        return URL(
            scheme=kwargs.get("scheme", self.scheme),
            host=kwargs.get("host", self.host),
            port=kwargs.get("port", self.port),
            path=kwargs.get("path", self.path),
            query_string=kwargs.get("query_string", self.query_string),
            fragment=kwargs.get("fragment", self.fragment),
        )

    def __str__(self) -> str:
        if self._full is None:
            qs = f"?{self.query_string}" if self.query_string else ""
            frag = f"#{self.fragment}" if self.fragment else ""
            self._full = f"{self.scheme}://{self.netloc}{self.path}{qs}{frag}"
        return self._full


# ── Multidict-backed collections ──────────────────────────
# These must be double-quoted (RFC 9110 Sec. 5.6.6); `\` is not a `tchar`.
_HEADER_PARAM_QUOTE_TRIGGERS = frozenset((" ", ";", ",", '"', "\\"))


class _GetListMixin:
    """Shared `getlist` for the multidict-backed collections.

    `multidict` exposes `getall`, which raises `KeyError` when the key is
    absent; `getlist` returns an empty list instead. Each subclass must declare
    its own `__slots__`, because one that omits it regains a per-instance
    `__dict__` regardless of the mixin's empty slots.
    """

    __slots__ = ()

    def getlist(self, key: str) -> list[str]:
        """Return all values for the given key as a list. Empty list if absent."""
        try:
            return self.getall(key)  # type: ignore[attr-defined]
        except KeyError:
            return []


def _files_key_hint(key: str, mimetype: str, form_keys: frozenset[str]) -> str:
    """Build the debug message for a missing `request.files` key.

    Branches on what the request actually carried, so the message names the
    real cause of the commonest upload mistakes.
    """
    if key in form_keys:
        return (
            f"No uploaded file named {key!r}. A form field with that name was "
            "received as a plain text value, not a file. The form was submitted "
            'without enctype="multipart/form-data", so no file contents were '
            f"transmitted (request mimetype: {mimetype!r}). Add that enctype to "
            "the form to upload files."
        )
    if mimetype == MIME_JSON:
        return (
            f"No uploaded file named {key!r}. This is a JSON request "
            f"({mimetype!r}); uploaded files only exist on "
            "multipart/form-data bodies. Read the value from the JSON body, or "
            "submit the file as multipart/form-data."
        )
    return (
        f"No uploaded file named {key!r}. The request did not include a "
        f"multipart/form-data body (request mimetype: {mimetype!r}), so no "
        "uploaded files are available."
    )


class FormData(_GetListMixin, MultiDict[Any]):
    """Multi-value form-field collection (text fields + file uploads).

    Backed by `multidict.MultiDict`. Repeated form fields (`<input name="a">`
    submitted twice, or repeated multipart parts with the same `name`)
    preserve every value; single-value access `form["a"]` returns the first.
    `getlist("a")` returns the full list.
    """

    # Set by `Request.files` under `debug=True`, so a missing key raises a
    # descriptive `FilesKeyError`; `None` costs a production lookup nothing.
    __slots__ = ("_files_diagnostic",)

    _files_diagnostic: tuple[str, frozenset[str]] | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._files_diagnostic = None

    def __getitem__(self, key: str) -> Any:
        try:
            return super().__getitem__(key)
        except KeyError:
            diag = self._files_diagnostic
            if diag is None:
                raise
            raise FilesKeyError(_files_key_hint(key, diag[0], diag[1])) from None

    def get_upload(self, key: str) -> UploadFile | None:
        """Return the first value if it is an `UploadFile`, else `None`."""
        val = self.get(key)
        return val if isinstance(val, UploadFile) else None


class Headers(_GetListMixin, CIMultiDict[str]):
    """Case-insensitive, multi-value header collection.

    Backed by `multidict.CIMultiDict`. `headers["X"]` returns the first value;
    `headers.getlist("X")` returns all of them. Construction from a plain dict,
    a list of tuples or another multidict all work.
    """

    __slots__ = ()

    def to_wsgi_list(self) -> list[tuple[str, str]]:
        """Return headers as a list of `(name, value)` tuples.

        Preserves insertion order and every duplicate.
        """
        return [(k, v) for k, v in self.items()]

    def copy(self) -> Headers:
        """Return a shallow copy - a fresh `Headers` with the same entries."""
        return Headers(self.to_wsgi_list())

    def add(self, key: str, value: str, **params: str) -> None:
        """Append a header, with optional `key=value` parameters.

        `headers.add("Content-Disposition", "attachment", filename="x.txt")`
        emits `attachment; filename="x.txt"`. Parameter values
        containing whitespace or punctuation are double-quoted.
        Underscores in parameter names map to hyphens.
        """
        if params:
            parts = [value]
            for pk, pv in params.items():
                pk = pk.replace("_", "-")
                sval = str(pv)
                if any(c in sval for c in _HEADER_PARAM_QUOTE_TRIGGERS):
                    sval = '"' + _quote_header_value(sval) + '"'
                parts.append(f"{pk}={sval}")
            value = "; ".join(parts)
        super().add(key, value)


# ── Parsed header values ──────────────────────────────────
def _is_byte_pos(value: str) -> bool:
    """True when `value` is a byte position - ASCII digits only, nothing else."""
    return value.isdigit() and value.isascii()


class RangeSpec:
    """Parsed `Range:` header (RFC 9110 Sec. 14.2).

    - `unit` is the range unit, e.g. `"bytes"` (the only commonly-used one).
    - `ranges` is a list of `(start, end)` tuples, with `None` standing in
      for an open endpoint:
        - `0-499`   -> `(0, 499)`
        - `1000-`   -> `(1000, None)` (open at the right)
        - `-500`    -> `(None, 500)`  (suffix-range - last 500 bytes)
    """

    __slots__ = ("unit", "ranges")

    def __init__(self, unit: str, ranges: list[tuple[int | None, int | None]]) -> None:
        self.unit = unit
        self.ranges = ranges

    @classmethod
    def parse(cls, header_value: str) -> RangeSpec | None:
        """Parse a `Range:` header, returning `None` for a missing or unparseable value."""
        if not header_value:
            return None
        if "=" not in header_value:
            return None
        unit, _, spec = header_value.partition("=")
        unit = unit.strip().lower()
        ranges: list[tuple[int | None, int | None]] = []
        for chunk in spec.split(","):
            chunk = chunk.strip()
            if not chunk or "-" not in chunk:
                continue
            start_s, _, end_s = chunk.partition("-")
            start_s = start_s.strip()
            end_s = end_s.strip()
            # RFC 9110 Sec. 14.1.1 spells a byte position `1*DIGIT`, where
            # `int()` also reads a sign, `_` separators and non-ASCII digits.
            if (start_s and not _is_byte_pos(start_s)) or (end_s and not _is_byte_pos(end_s)):
                continue
            if not start_s and not end_s:
                continue  # `-` alone is invalid
            ranges.append((int(start_s) if start_s else None, int(end_s) if end_s else None))
        if not ranges:
            return None
        return cls(unit=unit, ranges=ranges)

    def __repr__(self) -> str:
        return f"RangeSpec(unit={self.unit!r}, ranges={self.ranges!r})"


# Lower-cased type, subtype, the frozenset of media-type parameter pairs, and
# a specificity score ranking competing matches (RFC 9110 Sec. 12.5.1):
#   3 = `type/subtype` with parameters, 2 = `type/subtype`, 1 = `type/*`,
#   0 = `*/*`, -1 = invalid (e.g. `*/json`), which never matches.
_MimeKey = tuple[str, str, frozenset[tuple[str, str]], int]


@functools.lru_cache(maxsize=128)
def _lookup_mime_key(value: str) -> _MimeKey:
    """`_parse_mime_key` for a media range the caller names, memoized.

    `quality("application/json")` asks about a literal the application owns,
    and re-parsing it per call measured three parses on one MCP request.
    Deliberately NOT used for the ranges parsed out of an `Accept` header: those
    are remote input, and a few hundred distinct ones would evict the handful
    this cache exists to hold.
    """
    return _parse_mime_key(value)


def _parse_mime_key(value: str) -> _MimeKey:
    """Decompose a media range into a comparison-ready `_MimeKey`."""
    head, _, rest = value.partition(";")
    head = head.strip().lower()
    type_, slash, subtype = head.partition("/")
    if not slash:
        # No `/` - not a valid media range.
        return ("", "", frozenset(), -1)
    type_ = type_.strip()
    subtype = subtype.strip()
    # `*/non-*` is meaningless (RFC 9110 Sec. 12.5.1) - never match it.
    if type_ == "*" and subtype != "*":
        return ("", "", frozenset(), -1)
    # Parameter names are case-insensitive; values keep all but their quotes.
    params: set[tuple[str, str]] = set(parse_media_type_params(rest))
    if type_ == "*":
        specificity = 0
    elif subtype == "*":
        specificity = 1
    elif params:
        specificity = 3
    else:
        specificity = 2
    return (type_, subtype, frozenset(params), specificity)


class AcceptHeader:
    """Parsed `Accept-*` header with RFC 9110 Sec. 12.5 q-value semantics.

    Construction is via `AcceptHeader.parse(raw, mime=False)`. `mime=True`
    enables MIME-style wildcard matching (`text/*`, `*/*`) used by
    `Accept`; defaults to plain string equality used by `Accept-Language`,
    `Accept-Encoding`, `Accept-Charset`.
    """

    __slots__ = ("_options", "_mime")

    def __init__(self, options: list[tuple[str, float]], mime: bool) -> None:
        # Never re-sorted: `best_match` breaks ties on the configured option
        # order. A MIME option is decomposed once into a cached `_MimeKey`.
        self._mime = mime
        self._options: list[tuple[str, float, _MimeKey | None]]
        if mime:
            self._options = [(value, q, _parse_mime_key(value)) for value, q in options]
        else:
            self._options = [(value, q, None) for value, q in options]

    # ── Parsing ────────────────────────────────────────────

    @classmethod
    def parse(cls, raw: str, mime: bool = False) -> AcceptHeader:
        """Parse a comma-separated header into (value, q) tuples.

        A missing or unparseable q-value defaults to 1.0 (RFC 9110
        Sec. 12.4.2). An entry with `q=0` is kept, because `best_match` treats
        it as an explicit rejection. For MIME headers the media-type parameters
        are retained and participate in matching (RFC 9110 Sec. 12.5.1).
        """
        if not raw:
            return cls([], mime)
        items: list[tuple[str, float]] = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ";" not in chunk:
                items.append((chunk, 1.0))
                continue
            value, _, rest = chunk.partition(";")
            value = value.strip()
            q = 1.0
            # Media-type parameters appear BEFORE `q` and accept-extension ones
            # after (RFC 9110 Sec. 12.5.1); only the former take part in matching.
            params: list[str] = []
            seen_q = False
            for param in rest.split(";"):
                param = param.strip()
                if not param:
                    continue
                if not seen_q and (param.startswith("q=") or param.startswith("Q=")):
                    try:
                        q = float(param[2:])
                    except ValueError:
                        q = 1.0
                    # A non-finite q from `float("inf")` would poison every
                    # comparison, so it is treated as unparseable.
                    if not math.isfinite(q):
                        q = 1.0
                    seen_q = True
                    continue
                if not seen_q:
                    params.append(param)
            if mime and params:
                value = value + ";" + ";".join(params)
            items.append((value, q))
        return cls(items, mime)

    # ── Quality queries ────────────────────────────────────

    @property
    def values(self) -> list[str]:
        """All accepted values in the order the client sent them."""
        return [v for v, _, _ in self._options]

    def quality(self, value: str) -> float:
        """Return the q-value the client assigned to `value`.

        For MIME headers, matches `*/*` and `type/*` wildcards as well as
        parameterized media ranges; the MOST SPECIFIC matching client range wins
        (RFC 9110 Sec. 12.5.1), ties broken by the higher q-value. Returns 0 for
        a value that is rejected or not mentioned.
        """
        if self._mime:
            return self._mime_best(_lookup_mime_key(value))[0]
        folded = value.lower()
        best = 0.0
        for opt, q, _okey in self._options:
            if (opt.lower() == folded or opt == "*") and q > best:
                best = q
        return best

    def quality_explicit(self, value: str) -> float:
        """Return the q-value for `value`, with explicit tokens overriding `*`.

        RFC 9110 Sec. 12.5.3: an explicit `q=0` means "not acceptable" and must
        override a more permissive wildcard, which `quality()` does not do - it
        reports the MAX, so `br;q=0, *;q=1` gives `br` 1.0. This variant prefers
        an EXACT token match and falls back to the `*` q only for a value not
        explicitly listed. Non-MIME (Accept-Encoding) semantics.
        """
        # Content-coding tokens are case-insensitive (RFC 9110 Sec. 12.5.3 /
        # Sec. 8.4.1), so an explicit `Br;q=0` must reject `br`.
        folded = value.lower()
        # RFC 9110 leaves a repeated token with conflicting weights
        # (`gzip;q=1.0, gzip;q=0`) undefined; an explicit refusal anywhere wins.
        found: float | None = None
        for opt, q, _okey in self._options:
            if opt.lower() == folded:
                if q == 0.0:
                    return 0.0
                if found is None:
                    found = q
        if found is not None:
            return found
        for opt, q, _okey in self._options:
            if opt == "*":
                return q
        return 0.0

    def accepts_identity(self) -> bool:
        """Whether the `identity` (no-encoding) coding is acceptable per RFC 9110.

        RFC 9110 Sec. 12.5.3: `identity` is acceptable by default. It is
        UNacceptable only when an explicit `identity` entry carries `q=0`, or
        when it is unlisted and a `*` entry carries `q=0`. Token comparison is
        case-insensitive (Sec. 8.4.1).
        """
        for opt, q, _okey in self._options:
            if opt.lower() == "identity":
                # Explicit entry wins: identity is acceptable iff its q > 0.
                return q > 0
        for opt, q, _okey in self._options:
            if opt == "*":
                # Identity not named; the wildcard's q decides it.
                return q > 0
        # Neither identity nor `*` listed - identity stays acceptable by default.
        return True

    # ── Matching ───────────────────────────────────────────

    def best_match(self, options: list[str], default: str | None = None) -> str | None:
        """Return the option the client accepts with the highest q-value.

        Among candidates the client accepts (q>0), the one whose best matching
        range has the highest `(q, specificity)` wins, so a parameterized exact
        match beats a bare wildcard (RFC 9110 Sec. 12.5.1); ties go to the order
        in `options`. Returns `default` when no option has q>0, and `options[0]`
        for an empty header, since a missing Accept means "accept anything".
        """
        if not self._options:
            return options[0] if options else default
        best_opt: str | None = default
        best_rank: tuple[float, int] = (0.0, -1)
        for opt in options:
            rank = self._match_rank(opt)
            if rank[0] > 0.0 and rank > best_rank:
                best_rank = rank
                best_opt = opt
        return best_opt

    def _match_rank(self, value: str) -> tuple[float, int]:
        """Return `(quality, specificity)` for `value` against this header.

        `specificity` is the score of the most specific matching client range
        (0 for non-MIME headers), which `best_match` uses to prefer a
        parameterized exact match over a wildcard at equal quality.

        Non-MIME headers rank through `quality_explicit`, not `quality`, because
        RFC 9110 Sec. 12.5.3 makes an explicit `q=0` a refusal a wildcard must
        not override. The MIME path resolves this by most-specific-range.
        """
        if not self._mime:
            return (self.quality_explicit(value), 0)
        return self._mime_best(_lookup_mime_key(value))

    def _mime_best(self, vkey: _MimeKey) -> tuple[float, int]:
        """Return `(quality, specificity)` for `vkey` across this header's ranges.

        The most specific matching client range wins regardless of q; among
        equally-specific ranges the higher q wins (RFC 9110 Sec. 12.5.1).
        """
        best_q = 0.0
        best_spec = -1
        for _opt, q, okey in self._options:
            if okey is None or not self._mime_matches(okey, vkey):
                continue
            spec = okey[3]
            if spec > best_spec or (spec == best_spec and q > best_q):
                best_spec = spec
                best_q = q
        return (best_q, best_spec)

    @staticmethod
    def _mime_matches(okey: _MimeKey, vkey: _MimeKey) -> bool:
        """Whether a client media range `okey` matches a server media type `vkey`.

        Type and subtype must match or be covered by the range's `*` wildcard,
        and every parameter the range names must be present with an equal value
        in the server type (RFC 9110 Sec. 12.5.1). An invalid range never matches.
        """
        otype, osub, oparams, ospec = okey
        if ospec < 0:
            return False
        vtype, vsub, vparams, _vspec = vkey
        if otype != "*" and otype != vtype:
            return False
        if osub != "*" and osub != vsub:
            return False
        # `<=`: the range's params must be a subset of the server type's.
        return oparams <= vparams

    def __contains__(self, value: str) -> bool:
        return self.quality(value) > 0

    def __bool__(self) -> bool:
        return any(q > 0 for _, q, _ in self._options)

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)


class Authorization:
    """Parsed `Authorization` header.

    Two common shapes are first-class:
    - `Basic` (RFC 7617): `.type == "basic"`, `.username` + `.password` set.
    - `Bearer` (RFC 6750): `.type == "bearer"`, `.token` set.

    Other schemes (Digest per RFC 7616, Negotiate, custom) populate `.params`
    with the comma-separated key="value" parameters parsed from the credentials
    portion; `.type` is the scheme name lower-cased.

    `Authorization.from_header(value)` returns `None` for empty or malformed
    input rather than raising.
    """

    __slots__ = ("type", "scheme", "raw", "username", "password", "token", "params")

    def __init__(
        self,
        type: str,
        raw: str,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        params: dict[str, str] | None = None,
    ) -> None:
        self.type = type
        # `scheme` is the original casing (e.g. "Bearer"); `type` is lowercase.
        self.scheme = raw.split(" ", 1)[0] if " " in raw else raw
        self.raw = raw
        self.username = username
        self.password = password
        self.token = token
        self.params = params or {}

    @classmethod
    def from_header(cls, header_value: str) -> Authorization | None:
        """Parse an `Authorization:` header value. Returns None on miss."""
        if not header_value:
            return None
        if " " not in header_value:
            # Single-token form (rare; some custom schemes use it).
            return cls(type=header_value.lower(), raw=header_value)
        scheme, _, credentials = header_value.partition(" ")
        scheme_lower = scheme.lower()

        if scheme_lower == "basic":
            # Shared with `HTTPBasic`, so the two cannot disagree. `None` covers
            # undecodable base64 and a colon-less value (RFC 7617 Sec. 2).
            decoded_pair = _decode_basic_credentials(credentials.strip())
            if decoded_pair is None:
                return cls(type="basic", raw=header_value)
            user, pw = decoded_pair
            return cls(type="basic", raw=header_value, username=user, password=pw)

        if scheme_lower == "bearer":
            return cls(type="bearer", raw=header_value, token=credentials.strip())

        # Digest / Negotiate / custom: key=value pairs, else a raw `token`.
        if "=" in credentials:
            _, params = parse_header_params(credentials, delimiter=",", unescape=True)
            return cls(type=scheme_lower, raw=header_value, params=params)
        return cls(type=scheme_lower, raw=header_value, token=credentials.strip())

    def __repr__(self) -> str:
        if self.type == "basic":
            return f"Authorization(type='basic', username={self.username!r})"
        if self.type == "bearer":
            return "Authorization(type='bearer')"
        return f"Authorization(type={self.type!r})"


# ── Cookies & query parameters ────────────────────────────
class Cookies(_GetListMixin, MultiDict[str]):
    """Cookie collection parsed from the `Cookie` header.

    Built on `multidict.MultiDict`. Parsing delegates to `iter_cookies`
    (RFC 6265 Sec. 5.4), which percent-decodes values and collapses a duplicate
    name to its first occurrence.
    """

    __slots__ = ()

    @classmethod
    def from_cookie_header(cls, header_value: str) -> Cookies:
        """Parse a `Cookie:` header value into a `Cookies` mapping.

        Delegates to `iter_cookies`, which percent-decodes and strips quotes;
        a duplicate name collapses to its first occurrence (RFC 6265 Sec. 5.4).
        """
        return cls(iter_cookies(header_value))


class QueryParams(_GetListMixin, MultiDict[str]):
    """Multi-value, case-sensitive query parameter collection.

    Backed by `multidict.MultiDict`. Repeated query keys (``?x=1&x=2``)
    preserve every value; `getlist("x")` returns ``["1", "2"]`` while
    `params["x"]` returns ``"1"`` (the first).
    """

    __slots__ = ()

    @classmethod
    def from_query_string(cls, query_string: str) -> QueryParams:
        """Parse ``a=1&b=2&a=3`` into a multi-value mapping.

        Keeps blank values (``a=``) and decodes percent-escapes; repeated keys
        stay in the order the URL carried them.
        """
        if not query_string:
            return cls()
        try:
            items = _parse_qs_pairs(query_string, _MAX_QUERY_FIELDS)
        except ValueError as exc:
            # Surfaced as 414 rather than the `ValueError` from the cap.
            raise RequestURITooLong(f"Query string exceeds {_MAX_QUERY_FIELDS} fields") from exc
        return cls(items)
