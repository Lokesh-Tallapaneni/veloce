"""Core data structures — UploadFile, Header, URL, FormData.

`Headers` and `QueryParams` subclass `multidict.CIMultiDict` and
`multidict.MultiDict` respectively. They preserve duplicate keys and add
the `getlist` alias on top of multidict's native `getall`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import ipaddress
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
from veloce._header_parsing import parse_header_params
from veloce._internal import is_default_port
from veloce._protocol_constants import URL_SCHEME_HTTP, URL_SCHEME_HTTPS
from veloce.exceptions import FilesKeyError, RequestURITooLong
from veloce.http.cookies import iter_cookies

# Cap on number of query-string fields parsed per request to bound CPU
# and memory under hash-collision / parameter-pollution DoS.
_MAX_QUERY_FIELDS = 1000


# -- Request scope primitives ----------------------------------------
class Address(NamedTuple):
    """Client/server address - ASGI shape.

    A two-field named tuple so `request.client.host` /
    `request.client.port` work, while `host, port = request.client`
    unpacking also works (tuple semantics).
    """

    host: str
    port: int


class State(dict):
    """Per-request scratch namespace - supports both styles.

    ASGI servers expose `request.state` for attribute-style
    storage (`request.state.user = ...`). Veloce's dispatcher also
    stashes framework internals (`session`, `url_rule`, ...) here by
    key. `State` is a `dict` subclass whose attribute access maps to
    items, so `state.user` and `state["user"]` / `state.get("user")`
    are interchangeable - neither call site needs to know the other.
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


# -- Uploaded files --------------------------------------------------
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
        # Expose the part's headers as a case-insensitive `Headers` view, so a
        # handler can read `upload.headers["Content-Transfer-Encoding"]` even
        # though the parser stores keys lowercased. A plain dict passed by a
        # caller is normalised the same way.
        self.headers = headers if isinstance(headers, Headers) else Headers(headers or {})

    @property
    def content(self) -> bytes:
        """Return the full file content as bytes.

        Warning: this is a synchronous property. For large uploads that
        have been spooled to disk (i.e. ``_file_is_in_memory()`` returns
        False), the underlying ``read()`` call performs blocking I/O.
        Prefer ``await read()`` in async contexts for spooled files.
        """
        pos = self.file.tell()
        self.file.seek(0)
        data = self.file.read()
        self.file.seek(pos)
        return data

    def save(self, destination: str | BinaryIO, buffer_size: int = 16384) -> None:
        """Stream this upload into `destination`.

        - `destination` is either a filesystem path (`str`) or an already-open
          binary file object. With a path, the file is opened in `"wb"` mode
          and closed afterwards; with a file object, the caller stays
          responsible for closing it.
        - `buffer_size` controls the chunk size used while streaming -
          keeps memory bounded for large uploads without loading them
          fully into RAM.

        The upload's read cursor is reset to 0 before reading and restored
        to its prior position afterwards so the upload remains available
        for re-inspection.
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
            # If the underlying stream rejects re-seeking (closed,
            # one-shot stream, ...) just swallow - we're done with it.
            with contextlib.suppress(ValueError, OSError):
                self.file.seek(pos)

    async def read(self, size: int = -1) -> bytes:
        """Read up to size bytes from the upload."""
        # In-memory file objects stay on the loop; rolled-over spools
        # and arbitrary file-likes hop to a thread.
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

    def _file_is_in_memory(self) -> bool:
        """`True` when reads/writes are pure-Python memory ops.

        Both a `BytesIO` (the constructor default) *and* a
        `SpooledTemporaryFile` that has not rolled over to disk fall
        into this category - the multipart parser hands us the
        latter, and that's the production hot path. Once the spool
        rolls over to a real file, every op becomes a syscall and
        must go to a thread.

        `_rolled` is a `SpooledTemporaryFile` attribute (stdlib);
        anything else falls back to "treat as on-disk" for safety.
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


# -- URL -------------------------------------------------------------
# Safe default host used when the Host header fails validation. Matches the
# `URL.__init__` default so a rejected Host degrades to the same value as a
# missing one rather than leaking the attacker-controlled string.
_DEFAULT_HOST = "localhost"

# RFC 3986 Sec. 3.2.2 reg-name / unreserved + sub-delims, plus `%` for
# percent-encoded octets. A registered hostname is built only from these
# characters; anything else (`/`, `?`, `#`, `@`, whitespace, control bytes,
# CR/LF/NUL) is illegal in the authority's host and is exactly what a
# Host-injection payload (`evil.com/path?x`, `a\r\nX: y`) carries. Validating
# against this set stops such a Host from poisoning host / netloc / base_url /
# url_root / absolute-URL construction.
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

    Parsed with `ipaddress.IPv6Address` so malformed forms like `:::1`, `::::`,
    or `2001:::1` are rejected (a bare character-class check would accept them).
    """
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return False
    return True


def _parse_host_header(host_header: str) -> tuple[str, int | None]:
    """Split a Host header into a validated `(host, port)`.

    Rejects a Host whose host component is not a valid RFC 3986 reg-name or
    IPv6 literal, or whose port is not a 1-65535 integer, by falling back to
    `(_DEFAULT_HOST, None)`. A malformed Host therefore degrades to the safe
    default instead of flowing into absolute-URL construction, where it could
    inject a path/query/CRLF or an attacker-chosen origin.
    """
    if "[" in host_header:
        # Bracketed IPv6: [::1]:8080
        bracket_start = host_header.index("[")
        bracket_end = host_header.find("]")
        # The literal must open at the start and close before any port; a `[`
        # anywhere else, or a missing `]`, is malformed.
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
    ) -> URL:
        """Construct a URL from request headers and path components.

        `forwarded_port` is the public port a trusted reverse proxy supplied
        (via `ProxyFix` reading `X-Forwarded-Port` / `Forwarded host=...:port`).
        A port embedded in the Host header always wins; `forwarded_port` only
        fills in the port when the Host header carries none, so a proxy on a
        non-default port (e.g. 8443) survives into `netloc` / absolute URLs.
        """
        host_header = headers.get(HEADER_HOST, "localhost")
        # Precedence (ASGI Sec. HTTP scope): the scope's `scheme` is the
        # authoritative answer when one was supplied - that's
        # what uvicorn sets under TLS. `X-Forwarded-Proto` is a hint set
        # by reverse proxies and only meaningful when ProxyFix or similar
        # has trusted it. Plain `http` is the final fallback.
        if scope_scheme:
            scheme = scope_scheme
        elif headers.get(HEADER_X_FORWARDED_PROTO) == URL_SCHEME_HTTPS:
            scheme = URL_SCHEME_HTTPS
        else:
            scheme = URL_SCHEME_HTTP
        # Validate the Host against the RFC 3986 Sec. 3.2.2 host grammar before
        # it reaches host / netloc / base_url / url_root. A malformed Host
        # (`evil.com/path?x`, a CRLF-injected value, a non-numeric port) falls
        # back to the safe default rather than poisoning absolute-URL
        # construction. A port embedded in the Host header still wins over
        # `forwarded_port`.
        host, port = _parse_host_header(host_header)
        # The Host header is the authoritative source of the port; only when
        # it carries none does a trusted `X-Forwarded-Port` / `Forwarded`
        # port fill in. This keeps an explicit `host:port` from being
        # overridden by a stale forwarded port.
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


# -- Multidict-backed collections ------------------------------------
# A header parameter value carrying any of these characters must be
# double-quoted (RFC 9110 Sec. 5.6.6 quoted-string). Hoisted so `Headers.add`
# does not rebuild the trigger set on every parameter.
_HEADER_PARAM_QUOTE_TRIGGERS = frozenset((" ", ";", ",", '"'))


class _GetListMixin:
    """Shared `getlist` for the multidict-backed collections.

    `multidict` exposes `getall`, which raises `KeyError` when the key is
    absent; the framework's collections expose `getlist`, which returns an
    empty list instead. The mixin is slotless so it adds no per-instance
    `__dict__` to its (also slotless-by-omission) multidict subclasses.
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

    Branches on what the request actually carried so the message names the
    real root cause of the most common upload mistakes.
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


class FormData(_GetListMixin, MultiDict):
    """Multi-value form-field collection (text fields + file uploads).

    Backed by `multidict.MultiDict`. Repeated form fields (`<input name="a">`
    submitted twice, or repeated multipart parts with the same `name`)
    preserve every value; single-value access `form["a"]` returns the first.
    `getlist("a")` returns the full list.
    """

    # Debug-only diagnostic recorded by `Request.files` when the app runs
    # with `debug=True`: `(request_mimetype, frozenset_of_form_field_names)`.
    # When set, a missing-key lookup raises a descriptive `FilesKeyError`
    # instead of a bare `KeyError`. `None` (the default) keeps plain
    # multidict semantics, so production lookups pay nothing.
    _files_diagnostic: tuple[str, frozenset[str]] | None = None

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


class Headers(_GetListMixin, CIMultiDict):
    """Case-insensitive, multi-value header collection.

    Backed by `multidict.CIMultiDict`. Existing single-value access via
    `headers["X"]` returns the first value (multidict semantics); use `headers.getlist("X")` to get all
    values. Construction from a plain dict, a list of tuples, or another
    multidict all work - the underlying constructor handles each shape.
    """

    def to_wsgi_list(self) -> list[tuple[str, str]]:
        """Return headers as a list of `(name, value)` tuples.

        Preserves insertion order and every duplicate. Useful for
        emitting to a WSGI/ASGI layer or for round-tripping.
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
                    sval = '"' + sval.replace('"', '\\"') + '"'
                parts.append(f"{pk}={sval}")
            value = "; ".join(parts)
        super().add(key, value)


# -- Parsed header values --------------------------------------------
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
            try:
                start = int(start_s) if start_s else None
                end = int(end_s) if end_s else None
            except ValueError:
                continue
            if start is None and end is None:
                continue  # `-` alone is invalid
            ranges.append((start, end))
        if not ranges:
            return None
        return cls(unit=unit, ranges=ranges)

    def __repr__(self) -> str:
        return f"RangeSpec(unit={self.unit!r}, ranges={self.ranges!r})"


# A decomposed MIME media range: lower-cased type, subtype, the frozenset of
# `(name, value)` media-type parameter pairs, and a specificity score used to
# rank competing matches (RFC 9110 Sec. 12.5.1):
#   3 = full `type/subtype` carrying parameters
#   2 = full `type/subtype`
#   1 = `type/*`
#   0 = `*/*`
# An invalid range (e.g. `*/json`) scores -1 and never matches.
_MimeKey = tuple[str, str, frozenset[tuple[str, str]], int]


def _parse_mime_key(value: str) -> _MimeKey:
    """Decompose a media range into a cached, comparison-ready `_MimeKey`."""
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
    params: set[tuple[str, str]] = set()
    if rest:
        for chunk in rest.split(";"):
            chunk = chunk.strip()
            if "=" not in chunk:
                continue
            k, _, v = chunk.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                v = v[1:-1]
            # Parameter names are case-insensitive; values are kept verbatim
            # except for the surrounding quotes.
            params.add((k.strip().lower(), v))
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
        # `options` is already (value, q) tuples in original order; we
        # never re-sort because best_match wants the configured option
        # order to break ties, not the client's.
        #
        # For MIME headers each option is decomposed once into a
        # `_MimeKey` (type, subtype, frozenset of media-type params,
        # specificity score) so matching and specificity ranking read a
        # cached tuple instead of re-splitting strings per comparison.
        self._mime = mime
        self._options: list[tuple[str, float, _MimeKey | None]]
        if mime:
            self._options = [(value, q, _parse_mime_key(value)) for value, q in options]
        else:
            self._options = [(value, q, None) for value, q in options]

    @classmethod
    def parse(cls, raw: str, mime: bool = False) -> AcceptHeader:
        """Parse a comma-separated header into (value, q) tuples.

        Q-values missing or unparseable default to 1.0 (RFC 9110 Sec. 12.4.2).
        Entries with `q=0` are kept - `best_match` treats them as
        explicit rejections of that option. For MIME headers, media-type
        parameters (e.g. `application/json;profile="x"`) are retained and
        participate in matching (RFC 9110 Sec. 12.5.1); the `q` parameter
        separates the q-value from the media-type parameters.
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
            # Media-type parameters appear BEFORE `q`; accept-extension
            # parameters appear AFTER it (RFC 9110 Sec. 12.5.1). For MIME
            # headers the pre-`q` parameters are kept on the value so they
            # take part in matching; for plain headers they are ignored.
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
                    seen_q = True
                    continue
                if not seen_q:
                    params.append(param)
            if mime and params:
                value = value + ";" + ";".join(params)
            items.append((value, q))
        return cls(items, mime)

    @property
    def values(self) -> list[str]:
        """All accepted values in the order the client sent them."""
        return [v for v, _, _ in self._options]

    def quality(self, value: str) -> float:
        """Return the q-value the client assigned to `value`.

        For MIME headers, matches `*/*` and `type/*` wildcards as well as
        parameterized media ranges (e.g. `application/json;profile=x`); the
        MOST SPECIFIC matching client range wins (RFC 9110 Sec. 12.5.1), with
        ties broken by the higher q-value. Returns 0 when the value is
        rejected or not mentioned (callers usually special-case this).
        """
        if self._mime:
            return self._mime_best(_parse_mime_key(value))[0]
        folded = value.lower()
        best = 0.0
        for opt, q, _okey in self._options:
            if (opt.lower() == folded or opt == "*") and q > best:
                best = q
        return best

    def quality_explicit(self, value: str) -> float:
        """Return the q-value for `value`, with explicit tokens overriding `*`.

        RFC 9110 Sec. 12.5.3: an explicit `q=0` means "not acceptable" and must
        override a more permissive wildcard. `quality()` returns the MAX across
        an exact match and a `*` match, so for `br;q=0, *;q=1` it reports 1.0 for
        `br` - serving a rejected coding. This variant prefers an EXACT token
        match (so an explicit `q=0` excludes the coding) and only falls back to
        the `*` wildcard q when `value` is not explicitly listed. Used by
        precompressed static selection where honoring an explicit rejection
        matters; non-MIME (Accept-Encoding) semantics.
        """
        # RFC 9110 Sec. 12.5.3 / Sec. 8.4.1: content-coding tokens are
        # case-insensitive, so `BR` must match an explicit `br` entry and an
        # explicit `Br;q=0` must reject `br`. Fold both sides to lowercase
        # before the exact compare; the `*` wildcard fallback is unaffected.
        folded = value.lower()
        for opt, q, _okey in self._options:
            if opt.lower() == folded:
                return q
        for opt, q, _okey in self._options:
            if opt == "*":
                return q
        return 0.0

    def accepts_identity(self) -> bool:
        """Whether the `identity` (no-encoding) coding is acceptable per RFC 9110.

        RFC 9110 Sec. 12.5.3: `identity` is acceptable by default unless it is
        explicitly excluded. It is UNacceptable only when an explicit `identity`
        entry carries `q=0`, OR when `identity` is not explicitly listed and a
        `*` wildcard entry carries `q=0` (the wildcard rejects every coding not
        named, including identity). A missing header, or any header that does
        not exclude identity, leaves identity acceptable. Token comparison is
        case-insensitive (Sec. 8.4.1). Used by precompressed static selection to
        decide between serving the uncompressed asset and returning 406.
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

    def best_match(self, options: list[str], default: str | None = None) -> str | None:
        """Return the option the client accepts with the highest q-value.

        Among candidates the client accepts (q>0), the one whose best
        matching client range has the highest `(q, specificity)` wins, so a
        parameterized exact match beats a bare wildcard (RFC 9110
        Sec. 12.5.1). Ties on both go to the order in `options` (caller's
        preference). Returns `default` when no option has q>0. When the
        header is empty (no preference expressed), returns `options[0]` -
        a missing Accept means "accept anything".
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

        `specificity` is the score of the most specific matching client
        range (0 for non-MIME headers), used by `best_match` to prefer a
        parameterized exact match over a wildcard at equal quality.
        """
        if not self._mime:
            return (self.quality(value), 0)
        return self._mime_best(_parse_mime_key(value))

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

        Type and subtype must match or be covered by the range's `*`
        wildcard, and every media-type parameter named in the range must be
        present with an equal value in the server type (RFC 9110 Sec. 12.5.1).
        An invalid range (specificity -1) never matches.
        """
        otype, osub, oparams, ospec = okey
        if ospec < 0:
            return False
        vtype, vsub, vparams, _vspec = vkey
        if otype != "*" and otype != vtype:
            return False
        if osub != "*" and osub != vsub:
            return False
        # Parameters only constrain a fully-specified range; `<=` checks the
        # range's params are a subset of the server type's params.
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

    Other schemes (Digest per RFC 7616, Negotiate, custom) populate
    `.params` with the comma-separated key="value" parameters parsed
    from the credentials portion; `.type` is the scheme name lower-cased.

    Construction is via `Authorization.from_header(value)` which returns
    `None` for empty / malformed inputs rather than raising.
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
            try:
                decoded = base64.b64decode(credentials.strip(), validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return cls(type="basic", raw=header_value)
            if ":" in decoded:
                user, _, pw = decoded.partition(":")
                return cls(type="basic", raw=header_value, username=user, password=pw)
            return cls(type="basic", raw=header_value, username=decoded, password="")

        if scheme_lower == "bearer":
            return cls(type="bearer", raw=header_value, token=credentials.strip())

        # Digest / Negotiate / custom: parse comma-separated key=value pairs
        # if present; otherwise just keep the raw credentials in `token`.
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


# -- Cookies & query parameters --------------------------------------
class Cookies(_GetListMixin, MultiDict):
    """Cookie collection parsed from the `Cookie` header.

    Built on `multidict.MultiDict`. Parsing delegates to `iter_cookies`
    (RFC 6265 section 5.4) so values are percent-decoded. Duplicate names
    collapse to the first occurrence per the spec.
    """

    @classmethod
    def from_cookie_header(cls, header_value: str) -> Cookies:
        """Parse a `Cookie:` header value into a `Cookies` mapping.

        Delegates to `iter_cookies` for RFC 6265-compliant parsing
        (percent-decoding, quote-stripping). Duplicate names collapse
        to the first occurrence per RFC 6265 section 5.4.
        """
        return cls(iter_cookies(header_value))


class QueryParams(_GetListMixin, MultiDict):
    """Multi-value, case-sensitive query parameter collection.

    Backed by `multidict.MultiDict`. Repeated query keys (``?x=1&x=2``)
    preserve every value; `getlist("x")` returns ``["1", "2"]`` while
    `params["x"]` returns ``"1"`` (the first).
    """

    @classmethod
    def from_query_string(cls, query_string: str) -> QueryParams:
        """Parse ``a=1&b=2&a=3`` into a multi-value mapping.

        Keeps blank values (``a=``) and decodes percent-escapes. The
        ordering of repeated keys reflects the order in the URL.
        """
        if not query_string:
            return cls()
        try:
            items = parse_qsl(
                query_string,
                keep_blank_values=True,
                max_num_fields=_MAX_QUERY_FIELDS,
            )
        except ValueError as exc:
            # parse_qsl raises when the field count exceeds the cap;
            # surface as 414 so the framework returns a clean response.
            raise RequestURITooLong(f"Query string exceeds {_MAX_QUERY_FIELDS} fields") from exc
        return cls(items)


# -- Backward-compatible re-exports ----------------------------------
# Re-export from formparsers for backward compatibility.
from veloce.http.formparsers import (  # noqa: E402, F401
    DEFAULT_MAX_MULTIPART_PART_SIZE,
    DEFAULT_MAX_MULTIPART_PARTS,
    parse_multipart_form,
)
