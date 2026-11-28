"""Response core — the slotted state, body, status, content type and wire encoding."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import veloce._json as _json
from veloce._constants import (
    HEADER_CONTENT_LENGTH,
    HEADER_CONTENT_TYPE,
    MIME_TEXT_PLAIN,
    MSG_LABEL_HEADER_NAME,
    MSG_LABEL_SET_COOKIE_VALUE,
)
from veloce._header_parsing import parse_media_type_params
from veloce._internal import (
    _STATUS_PHRASES,
    _encode_response_head,
    _header_value_has_crlf,
    _reject_header_crlf,
    is_json_mimetype,
)
from veloce._protocol_constants import (
    SET_COOKIE_JOINER,
)
from veloce.background import coerce_background

# ── Case-insensitive response-header access ──────────────────────────
# `Response.headers` is case-SENSITIVE but HTTP field names are not (RFC 9110
# Sec. 5.1), so these fold the lookup without allocating a dict per call.
from veloce.http.response._headers import (
    advertised_length,
    header_key,
)
from veloce.status import (
    HTTP_200_OK,
    status_permits_body,
)


class ResponseBase:
    """State and wire encoding every response shares."""

    __slots__ = (
        "status_code",
        "_body",
        "content_type",
        "headers",
        "_encoded",
        "background",
        "_stream",
        "_ct_cache_key",
        "_mimetype",
        "_charset",
        "_ct_params",
    )

    #: An SSE stream. A class attribute: a `getattr` default on slots costs ~38 ns.
    is_event_source = False

    def __init__(
        self,
        status_code: int = HTTP_200_OK,
        body: bytes = b"",
        content_type: str = MIME_TEXT_PLAIN,
        headers: dict[str, str] | None = None,
        background: Any = None,
    ) -> None:
        self.status_code = status_code
        self._encoded: bytes | None = None
        self._body = body
        self.content_type = content_type
        # Copied: a shared `HEADERS = {...}` would ship one user's session cookie on.
        self.headers = dict(headers) if headers else {}
        # Coerced, not stored raw: the dispatch cascade knows only `run` / `run_all`,
        # so another value would serve as if the task had run. The `None`
        # comparison measured 72 ns of a 432 ns `Response()`.
        self.background = None if background is None else coerce_background(background)
        # `StreamingResponse` rewrites this; `None` keeps `is_streamed` an attr load.
        self._stream: Any = None
        # `content_type` has no setter hook, so the parse cache keys on its value.
        self._ct_cache_key: str | None = None
        self._mimetype: str = ""
        self._charset: str = ""
        self._ct_params: dict[str, str] = {}

    @property
    def body(self) -> bytes:
        """Return the response body bytes."""
        return self._body

    @body.setter
    def body(self, value: bytes) -> None:
        """Set the response body, keeping any `Content-Length` in step.

        The single assignment `body`, `data` and `set_data` all go through, so
        no spelling can advertise a stale length and desynchronise a keep-alive
        connection. The constructor assigns `_body` directly, so a response that
        never reassigns its body pays nothing.
        """
        self._body = value
        stored = header_key(self.headers, HEADER_CONTENT_LENGTH)
        if stored is not None:
            self.headers[stored] = str(len(value))
        self._encoded = None

    @property
    def media_type(self) -> str:
        """Return the full Content-Type including parameters."""
        return self.content_type

    @media_type.setter
    def media_type(self, value: str) -> None:
        """Set the media type."""
        self.content_type = value
        self._encoded = None

    @property
    def is_json(self) -> bool:
        """True when `Content-Type` is JSON.

        Matches `application/json` and any `application/*+json`
        structured suffix (RFC 6839 Sec. 3.1).
        """
        return is_json_mimetype(self.mimetype)

    def get_json(self) -> Any:
        """Parse the response body as JSON.

        Returns `None` for an empty body. Useful in tests to inspect a
        JSON response without re-decoding `body` by hand. Raises if the
        body is non-empty and not valid JSON.
        """
        body = self.body
        return _json.loads(body) if body else None

    def _ensure_ct_parsed(self) -> None:
        """Parse `content_type` into mimetype / charset / params, value-keyed.

        Re-parses only when the current `content_type` differs from the value
        the cache was built from, so direct slot reassignment can never serve a
        stale result. A warm cache costs one string compare.
        """
        ct = self.content_type or ""
        if ct == self._ct_cache_key:
            return
        media, semi, rest = ct.partition(";")
        self._mimetype = media.strip().lower()
        params = dict(parse_media_type_params(rest)) if semi else {}
        self._ct_params = params
        # Off the shared parser, so the charset honours RFC 9110 Sec. 8.3.1 and 5.6.6.
        self._charset = params.get("charset", "utf-8")
        self._ct_cache_key = ct

    @property
    def mimetype(self) -> str:
        """The bare media type - `Content-Type` without parameters.

        `text/html; charset=utf-8` -> `text/html`. Lower-cased and
        stripped per RFC 9110 Sec. 8.3 (media types are case-insensitive).
        """
        self._ensure_ct_parsed()
        return self._mimetype

    @mimetype.setter
    def mimetype(self, value: str) -> None:
        """Set the mimetype."""
        # Read off the parsed map, so a `Charset=` carries (RFC 9110 Sec. 8.3.1).
        cs = self.charset
        had_charset = "charset" in self.mimetype_params
        self.content_type = f"{value}; charset={cs}" if had_charset else value
        self._encoded = None

    @property
    def status(self) -> str:
        """Full HTTP status line, e.g. `"200 OK"`.

        Assignable: accepts an int (`200`), a bare numeric string
        (`"200"`), or a full status line (`"200 OK"` / `"404 Not
        Found"`). The leading integer is parsed into `status_code`.
        """
        phrase = _STATUS_PHRASES.get(self.status_code, "")
        return f"{self.status_code} {phrase}".rstrip()

    @status.setter
    def status(self, value: int | str) -> None:
        """Set the status code."""
        if isinstance(value, int):
            self.status_code = value
        else:
            # Take the leading integer token of "404 Not Found" / "404".
            stripped = value.strip()
            if not stripped:
                raise ValueError("Response.status: empty value")
            head = stripped.split(None, 1)[0]
            self.status_code = int(head)
        self._encoded = None

    def encode(self, *, keep_alive: bool = True) -> bytes:
        """Encode to raw HTTP/1.1 bytes - called once, cached.

        `keep_alive` is what the transport decided about this connection. Only
        the keep-alive encode is cached, so a response re-encoded for a closing
        connection cannot serve a stale `Connection` line.
        """
        if keep_alive and self._encoded is not None:
            return self._encoded

        # Bodiless statuses (1xx/204/205/304) get no default content-type; a
        # handler-set one still wins. A 304 may advertise the would-be-200
        # `Content-Length` (RFC 9110 Sec. 8.6), while 1xx/204/205 advertise 0.
        body_allowed = status_permits_body(self.status_code)
        body = self.body if body_allowed else b""
        length = advertised_length(self.status_code, self.body)
        default_headers = {} if length is None else {HEADER_CONTENT_LENGTH: str(length)}
        if body_allowed:
            default_headers = {HEADER_CONTENT_TYPE: self.content_type, **default_headers}
        parts = _encode_response_head(
            self.status_code, default_headers, self.headers, keep_alive=keep_alive
        )
        parts.append("\r\n")

        encoded = "".join(parts).encode("latin-1") + body
        if keep_alive:
            self._encoded = encoded
        return encoded

    @property
    def content_length(self) -> int:
        """Length of the response body in bytes.

        Always derived from `len(body)`. Streaming responses (which
        don't materialise the body) return 0 here; see `is_streamed`.
        """
        return len(self.body)

    @property
    def is_streamed(self) -> bool:
        """`True` when the response body is a streaming iterator."""
        return self._stream is not None

    @property
    def charset(self) -> str:
        """Response charset from `Content-Type`.

        Falls back to `"utf-8"` when no charset parameter is present.
        Assignable: setting it rewrites the `charset=` parameter on the
        existing `Content-Type` (the bare media type is preserved).
        """
        self._ensure_ct_parsed()
        return self._charset

    @charset.setter
    def charset(self, value: str) -> None:
        """Set the charset."""
        ct = self.content_type or MIME_TEXT_PLAIN
        media = ct.split(";", 1)[0].strip()
        self.content_type = f"{media}; charset={value}"
        self._encoded = None

    @property
    def mimetype_params(self) -> dict[str, str]:
        """Parameters of the `Content-Type` header.

        Everything after the bare media type, as a dict of lower-cased
        parameter names to their (unquoted) values. For
        `text/html; charset=utf-8` this is `{"charset": "utf-8"}`.
        Returns an empty dict when no parameters are present.
        """
        self._ensure_ct_parsed()
        return self._ct_params

    def calculate_content_length(self) -> int:
        """Set `Content-Length` from `len(body)` and return the value.

        The emit paths compute `Content-Length` from `body` anyway; this is for
        callers that want it locked into `self.headers` ahead of time.
        """
        n = len(self.body)
        self.headers[HEADER_CONTENT_LENGTH] = str(n)
        self._encoded = None
        return n

    @property
    def headerlist(self) -> list[tuple[str, str]]:
        """Headers flattened to the `(name, value)` tuple list the wire emit sends.

        Each `Set-Cookie` (multi-cookie join) expands to its own tuple, so
        the caller gets the per-cookie view ASGI requires. Two spellings of one
        field name collapse to a single entry carrying the last name and value
        seen, at the position of the first (RFC 9110 Sec. 5.1 makes field names
        case-insensitive), and a CR/LF/NUL anywhere in a name or value raises
        `ValueError` rather than being handed back - both matching the emit
        paths. `Response.headers` remains the raw, unfolded view.
        """
        result: list[tuple[str, str]] = []
        slot_by_name: dict[str, int] = {}
        for k, v in self.headers.items():
            k_lower = k.lower()
            if k_lower == "set-cookie":
                for piece in v.split(SET_COOKIE_JOINER):
                    cookie = piece.strip()
                    _reject_header_crlf(cookie, MSG_LABEL_SET_COOKIE_VALUE)
                    result.append((k, cookie))
            else:
                # The f-string label is built only on the path that raises.
                if _header_value_has_crlf(k) or _header_value_has_crlf(v):
                    _reject_header_crlf(k, MSG_LABEL_HEADER_NAME)
                    _reject_header_crlf(v, f"{k} header value")
                slot = slot_by_name.get(k_lower)
                if slot is None:
                    slot_by_name[k_lower] = len(result)
                    result.append((k, v))
                else:
                    result[slot] = (k, v)
        return result

    @property
    def data(self) -> bytes:
        """Body bytes alias for `Response.body`."""
        return self.body

    @data.setter
    def data(self, value: bytes | str) -> None:
        """Set the response body; alias for `set_data`."""
        self.set_data(value)

    def set_data(self, value: bytes | str) -> None:
        """Replace the response body.

        Accepts `bytes` or `str` (UTF-8 encoded). Refreshes `Content-Length`
        when the headers carry one. Assigning `response.body` does the same.
        """
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.body = value

    def iter_encoded(self) -> Any:
        """Yield the response body.

        Return type is mode-dependent and the two modes are NOT
        interchangeable:

        - Buffered response (`is_streamed is False`) -> returns a
          synchronous iterator yielding `bytes`. Drain with `for`.
        - Streaming response (`is_streamed is True`) -> returns the
          underlying async iterator (`AsyncIterator[bytes]`). Drain
          with `async for`.

        Callers must branch on `response.is_streamed` (or use
        `inspect.isasyncgen` / `hasattr(it, "__aiter__")`) to pick
        the right loop, e.g.:

            it = response.iter_encoded()
            if response.is_streamed:
                async for chunk in it:
                    ...
            else:
                for chunk in it:
                    ...

        """
        stream = self._stream
        if stream is not None:
            return stream
        return iter([self.body]) if self.body else iter([])

    def iter_chunked(self, size: int) -> Any:
        """Yield the response body in fixed-size chunks.

        Return type is mode-dependent and the two modes are NOT
        interchangeable:

        - Buffered response (`is_streamed is False`) -> returns a
          synchronous generator yielding `bytes` slices of length
          `size` (the final slice may be shorter). Drain with `for`.
        - Streaming response (`is_streamed is True`) -> returns the
          underlying async iterator unchanged (`AsyncIterator[bytes]`);
          `size` is ignored because chunk boundaries are controlled by
          the source generator, not the caller. Drain with `async for`.

        Pick the loop based on `response.is_streamed`, as `iter_encoded` shows.
        `size` must be positive.
        """
        if size <= 0:
            raise ValueError("iter_chunked size must be positive")
        stream = self._stream
        if stream is not None:
            return stream
        body = self.body
        return (body[i : i + size] for i in range(0, len(body), size))

    if TYPE_CHECKING:  # pragma: no cover
        # Type-only, because `serving/` must not import `sse`; no runtime method.
        async def stream_to(
            self,
            transport: Any,
            drain: Callable[[], Awaitable[None]] | None = None,
            *,
            keep_alive: bool = True,
        ) -> None: ...

    def _encode_streaming_head(self, default_headers: dict[str, str], keep_alive: bool) -> bytes:
        """Encode a streaming response's head, honouring the bodiless-status rule.

        RFC 9112 Sec. 6.1 forbids `Transfer-Encoding` on a 204, and a 204 that
        ships chunks desynchronises a keep-alive connection. `default_headers`
        is therefore dropped for `Content-Length: 0` on a bodiless status.
        """
        if not status_permits_body(self.status_code):
            default_headers = {HEADER_CONTENT_LENGTH: "0"}
        parts = _encode_response_head(
            self.status_code, default_headers, self.headers, keep_alive=keep_alive
        )
        parts.append("\r\n")
        return "".join(parts).encode("latin-1")
