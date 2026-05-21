"""Core data structures — UploadFile, Header, URL, FormData.

`Headers` and `QueryParams` subclass `multidict.CIMultiDict` and
`multidict.MultiDict` respectively. They preserve duplicate keys and add
the `getlist` alias on top of multidict's native `getall`.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Any, BinaryIO

from multidict import CIMultiDict, MultiDict


class UploadFile:
    """Uploaded file with an async read/write interface."""

    __slots__ = ("filename", "content_type", "file", "size", "headers")

    def __init__(
        self,
        filename: str,
        content_type: str = "application/octet-stream",
        file: BinaryIO | None = None,
        size: int = 0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.filename = filename
        self.content_type = content_type
        self.file = file or io.BytesIO()
        self.size = size
        self.headers = headers or {}

    async def read(self, size: int = -1) -> bytes:
        return self.file.read(size)

    async def write(self, data: bytes) -> int:
        return self.file.write(data)

    async def seek(self, offset: int) -> None:
        self.file.seek(offset)

    async def close(self) -> None:
        self.file.close()

    async def __aenter__(self) -> UploadFile:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    @property
    def content(self) -> bytes:
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
        - `buffer_size` controls the chunk size used while streaming —
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
            # one-shot stream, …) just swallow — we're done with it.
            import contextlib

            with contextlib.suppress(ValueError, OSError):
                self.file.seek(pos)

    def _stream_into(self, out: BinaryIO, buffer_size: int) -> None:
        while True:
            chunk = self.file.read(buffer_size)
            if not chunk:
                break
            out.write(chunk)

    def __repr__(self) -> str:
        return f"UploadFile(filename={self.filename!r}, content_type={self.content_type!r}, size={self.size})"


class URL:
    """Parsed URL with component access — lazily constructed."""

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
        scheme: str = "http",
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
    ) -> URL:
        host_header = headers.get("host", "localhost")
        # Precedence (ASGI §HTTP scope): the scope's `scheme` is the
        # authoritative answer when one was supplied — that's
        # what uvicorn sets under TLS. `X-Forwarded-Proto` is a hint set
        # by reverse proxies and only meaningful when ProxyFix or similar
        # has trusted it. Plain `http` is the final fallback.
        if scope_scheme:
            scheme = scope_scheme
        elif headers.get("x-forwarded-proto") == "https":
            scheme = "https"
        else:
            scheme = "http"
        if ":" in host_header:
            host, port_str = host_header.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                host = host_header
                port = None
        else:
            host = host_header
            port = None
        return cls(
            scheme=scheme,
            host=host,
            port=port,
            path=path,
            query_string=query_string,
        )

    @property
    def netloc(self) -> str:
        if self.port and self.port not in (80, 443):
            return f"{self.host}:{self.port}"
        return self.host

    def __str__(self) -> str:
        if self._full is None:
            qs = f"?{self.query_string}" if self.query_string else ""
            frag = f"#{self.fragment}" if self.fragment else ""
            self._full = f"{self.scheme}://{self.netloc}{self.path}{qs}{frag}"
        return self._full

    def replace(self, **kwargs: Any) -> URL:
        return URL(
            scheme=kwargs.get("scheme", self.scheme),
            host=kwargs.get("host", self.host),
            port=kwargs.get("port", self.port),
            path=kwargs.get("path", self.path),
            query_string=kwargs.get("query_string", self.query_string),
            fragment=kwargs.get("fragment", self.fragment),
        )


class FormData(MultiDict):
    """Multi-value form-field collection (text fields + file uploads).

    Backed by `multidict.MultiDict`. Repeated form fields (`<input name="a">`
    submitted twice, or repeated multipart parts with the same `name`)
    preserve every value; single-value access `form["a"]` returns the first.
    `getlist("a")` returns the full list.
    """

    def getlist(self, key: str) -> list:
        try:
            return self.getall(key)
        except KeyError:
            return []

    def get_upload(self, key: str) -> UploadFile | None:
        """Return the first value if it is an `UploadFile`, else `None`."""
        val = self.get(key)
        return val if isinstance(val, UploadFile) else None


class Headers(CIMultiDict):
    """Case-insensitive, multi-value header collection.

    Backed by `multidict.CIMultiDict`. Existing single-value access via
    `headers["X"]` returns the first value (multidict semantics); use `headers.getlist("X")` to get all
    values. Construction from a plain dict, a list of tuples, or another
    multidict all work — the underlying constructor handles each shape.
    """

    def getlist(self, key: str) -> list:
        """Alias for `getall`. Empty list if absent."""
        try:
            return self.getall(key)
        except KeyError:
            return []

    def to_wsgi_list(self) -> list[tuple[str, str]]:
        """Return headers as a list of `(name, value)` tuples.

        Preserves insertion order and every duplicate. Useful for
        emitting to a WSGI/ASGI layer or for round-tripping.
        """
        return [(k, v) for k, v in self.items()]

    def copy(self) -> Headers:
        """Return a shallow copy — a fresh `Headers` with the same entries."""
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
                if any(c in str(pv) for c in (" ", ";", ",", '"')):
                    pv = '"' + str(pv).replace('"', '\\"') + '"'
                parts.append(f"{pk}={pv}")
            value = "; ".join(parts)
        super().add(key, value)


class RangeSpec:
    """Parsed `Range:` header (RFC 9110 §14.2).

    - `unit` is the range unit, e.g. `"bytes"` (the only commonly-used one).
    - `ranges` is a list of `(start, end)` tuples, with `None` standing in
      for an open endpoint:
        - `0-499`   → `(0, 499)`
        - `1000-`   → `(1000, None)` (open at the right)
        - `-500`    → `(None, 500)`  (suffix-range — last 500 bytes)
    """

    __slots__ = ("unit", "ranges")

    def __init__(self, unit: str, ranges: list[tuple[int | None, int | None]]) -> None:
        self.unit = unit
        self.ranges = ranges

    @classmethod
    def parse(cls, header_value: str) -> RangeSpec | None:
        """Parse a `Range:` header. Returns `None` on a missing or
        unparseable value rather than raising."""
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


class AcceptHeader:
    """Parsed `Accept-*` header with RFC 9110 §12.5 q-value semantics.

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
        self._options = options
        self._mime = mime

    @classmethod
    def parse(cls, raw: str, mime: bool = False) -> AcceptHeader:
        """Parse a comma-separated header into (value, q) tuples.

        Q-values missing or unparseable default to 1.0 (RFC 9110 §12.4.2).
        Entries with `q=0` are kept — `best_match` treats them as
        explicit rejections of that option.
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
            for param in rest.split(";"):
                param = param.strip()
                if param.startswith("q=") or param.startswith("Q="):
                    try:
                        q = float(param[2:])
                    except ValueError:
                        q = 1.0
                    break
            items.append((value, q))
        return cls(items, mime)

    @property
    def values(self) -> list[str]:
        """All accepted values in the order the client sent them."""
        return [v for v, _ in self._options]

    def quality(self, value: str) -> float:
        """Return the q-value the client assigned to `value`.

        For MIME headers, matches `*/*` and `type/*` wildcards. Returns 0
        when the value is rejected or not mentioned (callers usually
        special-case this).
        """
        best = 0.0
        for opt, q in self._options:
            if self._matches(opt, value) and q > best:
                best = q
        return best

    def _matches(self, opt: str, value: str) -> bool:
        if opt == value:
            return True
        if not self._mime:
            return False
        # MIME wildcards: `*/*`, `text/*`.
        if opt == "*/*":
            return True
        if "/" not in opt or "/" not in value:
            return False
        opt_type, opt_sub = opt.split("/", 1)
        val_type, _val_sub = value.split("/", 1)
        return bool(opt_sub == "*" and opt_type == val_type)

    def best_match(self, options: list[str], default: str | None = None) -> str | None:
        """Return the option the client accepts with the highest q-value.

        Ties go to the order in `options` (caller's preference). Returns
        `default` when no option has q>0. When the header is empty (no
        preference expressed), returns `options[0]` — RFC 9110 §12.5.1
        treats a missing Accept as "accept anything".
        """
        if not self._options:
            return options[0] if options else default
        best_opt: str | None = default
        best_q = 0.0
        for opt in options:
            q = self.quality(opt)
            if q > best_q:
                best_q = q
                best_opt = opt
        return best_opt

    def __contains__(self, value: str) -> bool:
        return self.quality(value) > 0

    def __bool__(self) -> bool:
        return any(q > 0 for _, q in self._options)

    def __iter__(self):
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
            import base64

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
            params: dict[str, str] = {}
            for chunk in _split_authz_params(credentials):
                if "=" not in chunk:
                    continue
                k, _, v = chunk.partition("=")
                params[k.strip().lower()] = v.strip().strip('"')
            return cls(type=scheme_lower, raw=header_value, params=params)
        return cls(type=scheme_lower, raw=header_value, token=credentials.strip())

    def __repr__(self) -> str:
        if self.type == "basic":
            return f"Authorization(type='basic', username={self.username!r})"
        if self.type == "bearer":
            return "Authorization(type='bearer')"
        return f"Authorization(type={self.type!r})"


def _split_authz_params(value: str) -> list[str]:
    """Split `a=1, b="c,d", e=f` on commas not inside double-quotes."""
    out: list[str] = []
    in_quote = False
    buf: list[str] = []
    for ch in value:
        if ch == '"':
            in_quote = not in_quote
            buf.append(ch)
        elif ch == "," and not in_quote:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return out


class Cookies(MultiDict):
    """Multi-value cookie collection parsed from the `Cookie` header.

    RFC 6265 lets a single `Cookie` header carry multiple `name=value`
    pairs separated by `;`, and (technically) the same name can appear
    more than once. Real-world clients typically send each name once,
    but we don't collapse duplicates — `cookies.getlist("name")` returns
    every value, `cookies["name"]` returns the first.
    """

    def getlist(self, key: str) -> list:
        try:
            return self.getall(key)
        except KeyError:
            return []

    @classmethod
    def from_cookie_header(cls, header_value: str) -> Cookies:
        """Parse a `Cookie:` header value into a `Cookies` mapping.

        Splits on `;`, then on the first `=`. Whitespace around segments
        is trimmed. Cookie attributes that don't look like `name=value`
        are skipped.
        """
        cookies = cls()
        if not header_value:
            return cookies
        for chunk in header_value.split(";"):
            chunk = chunk.strip()
            if "=" not in chunk:
                continue
            name, _, value = chunk.partition("=")
            cookies.add(name.strip(), value.strip())
        return cookies


class QueryParams(MultiDict):
    """Multi-value, case-sensitive query parameter collection.

    Backed by `multidict.MultiDict`. Repeated query keys (``?x=1&x=2``)
    preserve every value; `getlist("x")` returns ``["1", "2"]`` while
    `params["x"]` returns ``"1"`` (the first).
    """

    def getlist(self, key: str) -> list:
        try:
            return self.getall(key)
        except KeyError:
            return []

    @classmethod
    def from_query_string(cls, query_string: str) -> QueryParams:
        """Parse ``a=1&b=2&a=3`` into a multi-value mapping.

        Keeps blank values (``a=``) and decodes percent-escapes. The
        ordering of repeated keys reflects the order in the URL.
        """
        from urllib.parse import parse_qsl

        if not query_string:
            return cls()
        items = parse_qsl(query_string, keep_blank_values=True)
        return cls(items)


# Multipart-parsing safety limits — guard against algorithmic-complexity
# DoS from a body crafted with pathologically many or oversized parts. A
# body within MAX_CONTENT_LENGTH can still carry millions of tiny parts;
# these caps bound the work and memory the parser will commit to.
DEFAULT_MAX_MULTIPART_PARTS = 1000
DEFAULT_MAX_MULTIPART_PART_SIZE = 10 * 1024 * 1024  # 10 MiB per part


def parse_multipart_form(
    body: bytes,
    content_type: str,
    *,
    max_parts: int = DEFAULT_MAX_MULTIPART_PARTS,
    max_part_size: int = DEFAULT_MAX_MULTIPART_PART_SIZE,
) -> FormData:
    """Parse multipart/form-data into FormData with UploadFile support.

    `max_parts` caps how many parts the form may contain and
    `max_part_size` caps each part's body size. Exceeding either raises
    `RequestEntityTooLarge` (413), so a maliciously structured form
    cannot exhaust memory or CPU even when its total size is within
    `MAX_CONTENT_LENGTH`.
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
    delimiter = f"--{boundary}".encode()
    parts = body.split(delimiter)

    # Count real parts as they are encountered and bail the moment the
    # cap is crossed — exact regardless of whether the body carries a
    # well-formed `--boundary--` epilogue, and it stops before the
    # expensive per-part decoding for everything past the limit.
    parts_seen = 0

    for part in parts[1:]:
        if part.strip() == b"--" or not part.strip():
            continue

        parts_seen += 1
        if parts_seen > max_parts:
            from veloce.exceptions import RequestEntityTooLarge

            raise RequestEntityTooLarge(f"multipart form exceeds the {max_parts}-part limit")
        if b"\r\n\r\n" not in part:
            continue

        header_section, body_section = part.split(b"\r\n\r\n", 1)
        # Strip trailing \r\n
        if body_section.endswith(b"\r\n"):
            body_section = body_section[:-2]

        if len(body_section) > max_part_size:
            from veloce.exceptions import RequestEntityTooLarge

            raise RequestEntityTooLarge(
                f"multipart part exceeds the {max_part_size}-byte size limit"
            )

        headers_text = header_section.decode("utf-8", errors="replace")
        disposition = ""
        part_content_type = "text/plain"
        for line in headers_text.split("\r\n"):
            line_lower = line.lower()
            if line_lower.startswith("content-disposition:"):
                disposition = line.split(":", 1)[1].strip()
            elif line_lower.startswith("content-type:"):
                part_content_type = line.split(":", 1)[1].strip()

        # Extract name and filename from disposition
        name = ""
        filename = None
        for token in disposition.split(";"):
            token = token.strip()
            if token.startswith("name="):
                name = token[5:].strip('"')
            elif token.startswith("filename="):
                filename = token[9:].strip('"')

        if not name:
            continue

        if filename is not None:
            # File upload — use `add()` so repeated `name` parts (multiple
            # `<input multiple>` files under one name) are preserved.
            file_obj = io.BytesIO(body_section)
            upload = UploadFile(
                filename=filename,
                content_type=part_content_type,
                file=file_obj,
                size=len(body_section),
            )
            result.add(name, upload)
        else:
            # Regular field — same: `add()` preserves duplicate names.
            result.add(name, body_section.decode("utf-8", errors="replace"))

    return result
