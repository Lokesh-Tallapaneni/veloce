"""Request body — buffered and streamed access, JSON, form and per-request state."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from multidict import MultiDict

import veloce._json as _json
from veloce._constants import (
    MIME_FORM_URLENCODED,
    MIME_MULTIPART_FORM_DATA,
)
from veloce._internal import (
    _close_form_uploads,
    _coerce_bool,
    json_body_refused,
)
from veloce.exceptions import BadRequest, RequestEntityTooLarge, UnsupportedMediaType
from veloce.http._body import ASGIBodySource
from veloce.http.datastructures import (
    FormData,
    State,
    UploadFile,
    _parse_qs_pairs,
)
from veloce.http.formparsers import (
    DEFAULT_MAX_MULTIPART_PART_SIZE,
    DEFAULT_MAX_MULTIPART_PARTS,
    parse_multipart_form,
)

if TYPE_CHECKING:  # pragma: no cover
    from veloce.sessions import Session
from veloce.http.request._base import (
    _UNPARSED,
)
from veloce.http.request._url import UrlMixin

_logger = logging.getLogger(__name__)


class BodyMixin(UrlMixin):
    """Body, form and state access."""

    __slots__ = ()

    # ── Request state and session ─────────────────────────
    @property
    def state(self) -> State:
        """Per-request scratch namespace - ASGI shape.

        Supports attribute access (`request.state.user = ...`) *and*
        dict access (`request.state["user"]`, `request.state.get(...)`).
        """
        return self._state

    @property
    def session(self) -> Session:
        """Access to the session dict.

        `SessionMiddleware` writes the parsed session into `_state["session"]`
        during `process_request`; this property is the accessor for it. Raises
        `RuntimeError` when the middleware has not run - which keeps "I forgot
        to add SessionMiddleware" from showing up as a confusing silent
        empty-dict.
        """
        if "session" not in self._state:
            raise RuntimeError(
                "Request.session is unavailable - install SessionMiddleware "
                "(`app.add_middleware(SessionMiddleware(secret_key=...))`) "
                "to enable session storage."
            )
        session = self._state["session"]
        # Marked accessed so `Vary: Cookie` is emitted only when touched; a plain
        # dict placed here has no `accessed` slot.
        if hasattr(session, "accessed"):
            session.accessed = True
        return session

    # ── Body access ───────────────────────────────────────
    @property
    def data(self) -> bytes:
        """Raw request body bytes - `request.data` shape.

        The sync-property form of `body()`. Returns the body exactly as
        received, with no decoding or form parsing. Requires the body to
        already be buffered; raises `RuntimeError` otherwise (use
        `await request.body()` for the async path).
        """
        if not self._body_drained:
            raise RuntimeError(
                "Request body is not yet buffered; use `await request.body()` "
                "instead of the sync `request.data` accessor"
            )
        return self._body

    def _config(self) -> Any:
        """Return the bound app's `config` mapping, or `None`.

        `getattr(None, ...)` is safe, so this also covers the unbound-app
        case without a separate guard. Cold path - only the body/form/json
        accessors read per-app limit knobs.
        """
        return getattr(self.app, "config", None)

    @property
    def max_content_length(self) -> int | None:
        """The body-size cap for this request.

        Reads `app.config["MAX_CONTENT_LENGTH"]` from the bound app
        (the dispatcher enforces it, returning 413 on overflow).
        `None` - no limit - when unset or no app is bound.
        """
        cfg = self._config()
        return cfg.get("MAX_CONTENT_LENGTH") if cfg is not None else None

    def get_json(
        self,
        *,
        force: bool = False,
        silent: bool = False,
        cache: bool = True,
    ) -> Any:
        """Parse the request body as JSON.

        A body whose `Content-Type` is not JSON raises `UnsupportedMediaType`
        (415) through `on_json_loading_failed` rather than reading as `None`, so
        a content-type mismatch is reported instead of leaving the caller to
        infer it from a missing value. An absent header is not JSON either: a
        cross-origin `fetch` can omit it with no preflight, so a body sent
        without one is refused like any other non-JSON body. Pass `force=True`
        to read a body from a client that does not send the header.

        - `force=True` skips the content-type check entirely, parsing whatever
          the body holds.
        - `silent=True` returns `None` for both failures - a refused content
          type and a malformed body - instead of raising. Default raises so
          caller code can tell the two apart.
        - `cache=False` forces a re-parse on every call. Default caches
          the parsed value (one parse per request); cache invalidation
          is the caller's job when `cache=False`.

        Returns `None` for empty bodies regardless of `force` / `silent`.
        The content-type check runs first, so an empty body under a refused
        content type still raises.

        This is the synchronous accessor: it requires the
        body to already be buffered (the in-memory path), and raises
        `RuntimeError` otherwise - reach for `await request.json()` when
        the body has not yet been drained.
        """
        if not self._body_drained:
            raise RuntimeError(
                "Request body is not yet buffered; use `await request.json()` "
                "instead of the sync `request.get_json()` accessor"
            )
        if not force and json_body_refused(self.mimetype):
            if silent:
                return None
            return self.on_json_loading_failed(None)
        body = self._body
        if not body:
            return None

        if cache and self._json is not _UNPARSED:
            return self._json

        try:
            parsed = _json.loads(body)
        except _json.JSONDecodeError as exc:
            if silent:
                return None
            return self.on_json_loading_failed(exc)

        if cache:
            self._json = parsed
        return parsed

    def on_json_loading_failed(self, error: Exception | None) -> Any:
        """Handle a failure to read the body as JSON on a non-silent request.

        `error` is the decoder error, or `None` when the request declared a
        content type that is not JSON. The two failures answer with different
        status codes so a caller can tell them apart: a refused content type
        raises `UnsupportedMediaType` (415), a malformed body `BadRequest`
        (400).

        Raises `BadRequest` (400) with a stable, body-independent message so a
        malformed body cannot leak decoder internals (byte offsets derived from
        attacker-controlled input) into the production response. The verbose
        decoder reason is always logged and attached as `BadRequest.debug_detail`
        for operators, and is surfaced in the response only when debug mode or
        the `JSON_ERRORS_VERBOSE` config flag is set. Override on a `Request`
        subclass to customise.
        """
        if error is None:
            # RFC 9110 Sec. 15.5.16: 415 is for a media type the target rejects.
            raise UnsupportedMediaType(f"Expected a JSON body; got Content-Type {self.mimetype!r}")
        _logger.warning("JSON parse error: %s", error)
        cfg = self._config()
        # An explicit OR, since `JSON_ERRORS_VERBOSE` is seeded into the default
        # config and so never absent for a fallback to consult `DEBUG`.
        verbose = (
            _coerce_bool(cfg.get("JSON_ERRORS_VERBOSE", False))
            or _coerce_bool(cfg.get("DEBUG", False))
            if cfg
            else False
        )
        detail = f"Invalid JSON body: {error}" if verbose else "Invalid JSON body"
        exc = BadRequest(detail)
        # Informational only, and never read by the error renderer.
        setattr(exc, "debug_detail", str(error))  # noqa: B010 - a dynamic attribute name keeps the exception slot-free
        raise exc from error

    def _mark_body_buffered(self) -> None:
        """Publish an already-complete body without awaiting the source.

        The raw transport builds a `Request` at headers-complete and only then
        feeds the body, so a non-streaming route has to buffer before dispatch
        for the sync `.data` / `.get_json()` / `.form` accessors to see it. When
        the whole request arrived in one segment - the common case - the source
        is already at EOF with nothing queued, and awaiting it would cost a
        coroutine per request to learn there is nothing to wait for. The caller
        checks `source.at_eof` and calls this instead.
        """
        self._body_drained = True

    async def _drain_body(self) -> bytes:
        """Pull the body source to EOF once and cache the assembled bytes.

        In-memory requests are pre-filled at construction, so the first
        drain resolves immediately. Streamed requests (raw HTTP/1.1) pull the
        `RequestBodySource` to EOF here. The result is cached on `_body`;
        later drains return the cache.
        """
        if not self._body_drained:
            if self._body_source is not None:
                self._body = await self._body_source.read()
            self._body_drained = True
        return self._body

    async def body(self) -> bytes:
        """Return the full request body as bytes, draining the source once.

        Async to match the ASGI convention. Veloce buffers the
        body before dispatch, so no I/O happens inside the await - the
        coroutine resolves immediately with the cached bytes.
        """
        return await self._drain_body()

    async def json(self) -> Any:
        """Parse the request body as JSON, async to match the ASGI convention.

        Veloce buffers the body at construction time, so no I/O actually
        happens inside the await - the coroutine resolves immediately
        with the cached parse. The async signature exists so the
        `await request.json()` idiom does not blow up at runtime.

        A body whose `Content-Type` is not JSON reads as `None` rather than
        being parsed, which is what stops a cross-origin `text/plain` send from
        driving a JSON endpoint. An absent header is not JSON either, and is
        refused the same way: a `fetch` whose body is a `Blob` typed `""` sends
        no header at all, with no preflight to stop it.

        The synchronous `request.get_json()` accessor is available for callers
        that prefer a sync API. It applies the same rule but reports a refused
        content type as `UnsupportedMediaType` (415) instead of `None`; this
        door keeps the `None` so a caller reading the body itself can fall
        through to another representation.
        """
        if self._json is _UNPARSED:
            if json_body_refused(self.mimetype):
                return None
            body = await self._drain_body()
            if not body:
                self._json = None
            else:
                try:
                    self._json = _json.loads(body)
                except (_json.JSONDecodeError, ValueError) as exc:
                    # Share the single masking policy with the sync `get_json`.
                    return self.on_json_loading_failed(exc)
        return self._json

    async def get_data(self, *, as_text: bool = False, cache: bool = True) -> bytes | str:
        """Return the raw request body, draining the source once.

        Async to match `body()` / `json()` / `form()`.

        - `as_text=True` decodes via the `Content-Type` charset (default
          UTF-8). Falls back to `latin-1` when the declared charset is
          unrecognised - a defensive fallback, since
          latin-1 round-trips arbitrary bytes without raising.
        - `cache` is accepted and ignored. A non-streaming route has its body
          buffered before the handler runs, so there is nothing to decide; a
          `stream=True` route is consumed through `request.stream()` rather
          than here. The parameter is kept so callers passing `cache=False`
          for cross-framework compatibility keep working.

        Returns `bytes` (default) or `str` (with `as_text=True`).
        """
        _ = cache  # accepted for compatibility; see the note above
        body = await self._drain_body()
        if not as_text:
            return body
        charset = self.mimetype_params.get("charset", "utf-8")
        try:
            return body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            return body.decode("latin-1")

    async def form(self) -> FormData:
        """Parse form data including file uploads."""
        if self._form is None:
            mt = self.mimetype
            if mt == MIME_FORM_URLENCODED:
                body = await self._drain_body()
                # Caps the field count within MAX_CONTENT_LENGTH; `None` disables.
                max_fields = DEFAULT_MAX_MULTIPART_PARTS
                cfg = self._config()
                if cfg is not None:
                    max_fields = cfg.get("MAX_FORM_PARTS", max_fields)
                # Outside the field-count guard, since `UnicodeDecodeError`
                # subclasses `ValueError` and would be misreported as a 413.
                try:
                    decoded = body.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise BadRequest("form body is not valid UTF-8") from exc
                try:
                    items = _parse_qs_pairs(decoded, max_fields)
                except ValueError as exc:
                    raise RequestEntityTooLarge(
                        f"form exceeds the {max_fields}-field limit"
                    ) from exc
                self._form = FormData(items)
            elif mt == MIME_MULTIPART_FORM_DATA:
                # From config when an app is bound, else the module defaults.
                max_parts: int | None = DEFAULT_MAX_MULTIPART_PARTS
                max_part_size = DEFAULT_MAX_MULTIPART_PART_SIZE
                mp_max_files: int | None = None
                mp_max_fields: int | None = None
                mp_max_file_size: int | None = None
                mp_max_field_size: int | None = None
                mp_max_field_memory: int | None = None
                cfg = self._config()
                if cfg is not None:
                    # `None` disables it here too, so one value covers both.
                    max_parts = cfg.get("MAX_FORM_PARTS", max_parts)
                    cfg_part_size = cfg.get("MAX_FORM_PART_SIZE", max_part_size)
                    if cfg_part_size is not None:
                        max_part_size = cfg_part_size
                    mp_max_files = cfg.get("MAX_FORM_FILES")
                    mp_max_fields = cfg.get("MAX_FORM_FIELDS")
                    mp_max_file_size = cfg.get("MAX_FORM_FILE_SIZE")
                    mp_max_field_size = cfg.get("MAX_FORM_FIELD_SIZE")
                    mp_max_field_memory = cfg.get("MAX_FORM_FIELD_MEMORY")
                multipart_body = await self._drain_body()
                self._form = parse_multipart_form(
                    multipart_body,
                    self.content_type,
                    max_parts=max_parts,
                    max_files=mp_max_files,
                    max_fields=mp_max_fields,
                    max_part_size=max_part_size,
                    max_file_size=mp_max_file_size,
                    max_field_size=mp_max_field_size,
                    max_field_memory=mp_max_field_memory,
                )
            else:
                self._form = FormData()
        return self._form

    async def files(self) -> FormData:
        """View of uploaded files only - a `FormData` subset.

        Parses the form (via `form()`) and returns a `FormData`
        containing just the entries whose value is an `UploadFile`.
        Non-file form fields are excluded. Empty `FormData` for
        non-multipart requests. Result is cached after first parse.
        """
        if self._files is not None:
            return self._files
        form = await self.form()
        files = FormData()
        non_file_keys: set[str] = set()
        for key, value in form.items():
            if isinstance(value, UploadFile):
                files.add(key, value)
            else:
                non_file_keys.add(key)
        # Under `app.debug`, so a missing `request.files` key raises a descriptive
        # `FilesKeyError` rather than a bare `KeyError`.
        if self.app is not None and getattr(self.app, "debug", False):
            files._files_diagnostic = (self.mimetype, frozenset(non_file_keys))
        self._files = files
        return self._files

    async def values(self) -> MultiDict[Any]:
        """Merge the query string and form body - the `request.values` shape.

        Returns a fresh `MultiDict` with query-string entries first,
        then form-body entries appended. Both source `MultiDict`s
        preserve repeated keys; merging preserves the order across
        both sources. Form parsing is async (multipart may need
        executor reads), so this is an awaitable rather than a property.
        """
        merged: MultiDict[Any] = MultiDict()
        for k, v in self.query_params.items():
            merged.add(k, v)
        for k, v in (await self.form()).items():
            merged.add(k, v)
        return merged

    async def text(self) -> str:
        """Return body as text, draining the source once."""
        body = await self._drain_body()
        return body.decode("utf-8", errors="replace")

    def _close_uploads(self) -> None:
        """Close the spool files this request's multipart parse opened.

        An upload larger than the spool threshold is a real file on disk, and
        nothing closed it once the response was sent - the descriptor and the
        temp file survived until the garbage collector happened to reach the
        `UploadFile`. Under sustained upload traffic that is descriptor
        exhaustion. Called once the request is finished with, which is after
        any background task has run.
        """
        _close_form_uploads(self._form)

    async def is_disconnected(self) -> bool:
        """Whether the client has gone away.

        Answers for a handler that is still streaming its response - the SSE and
        long-poll case - as well as for a `stream=True` request body. On an ASGI
        server the receive channel is polled, without blocking and without
        consuming a message. Once the answer is `True` it stays `True`.

        **On the native transport a streaming handler does not observe this.**
        `connection_lost` records the drop and then cancels the task serving
        the request in the same callback, so the handler's next resume raises
        `CancelledError` at the point it would have polled. The handler does
        stop producing for a client that is gone - that part works - but
        through cancellation rather than through this method, so write the
        cleanup in a `finally` rather than behind an `is_disconnected()` check
        if the native server is the target.
        """
        source = self._body_source
        if source is None:
            receive = self._receive
            if receive is None:
                # No channel to ask: a request built outside a transport.
                return False
            # The source a buffered route skipped, built on the first poll and
            # latching its answer; the body accessors already hold their bytes.
            source = ASGIBodySource(receive, body_complete=True)
            self._body_source = source
        return await source.poll_disconnected()

    async def stream(self) -> AsyncIterator[bytes]:
        """Async-iterate the request body in chunks - ASGI shape.

        Streamed requests (raw HTTP/1.1) yield each chunk as the socket
        delivers it, so `async for chunk in request.stream(): ...` processes
        a large body incrementally without ever buffering it whole. For
        in-memory requests (TestClient / ASGI) the buffered bytes are sliced
        into 64 KiB chunks instead.

        A streamed body is consume-once: nothing is retained as it passes, so a
        later `request.body()` on a `stream=True` route sees an empty body
        rather than a copy the route asked not to keep.
        """
        if self._body_source is not None and not self._body_drained:
            # Held nowhere: keeping the chunks to back a later `.body()` retained
            # 33.6 MB for a 32 MiB upload, the profile this route opted out of.
            async for chunk in self._body_source:
                yield chunk
            self._body_drained = True
            return
        body = await self._drain_body()
        chunk_size = 65536
        for offset in range(0, len(body), chunk_size):
            yield body[offset : offset + chunk_size]
