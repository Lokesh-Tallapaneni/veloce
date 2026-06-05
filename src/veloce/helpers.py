"""Helpers - abort, jsonify, make_response, flash, g, current_app, send_from_directory."""

from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Sequence
from http import HTTPStatus
from typing import Any, NoReturn

from veloce._constants import (
    HEADER_CACHE_CONTROL,
    HEADER_ETAG,
    HEADER_LAST_MODIFIED,
    MSG_ACCESS_DENIED,
)
from veloce._internal import MIME_HTML, MIME_OCTET
from veloce.exceptions import exception_for_status
from veloce.http.dates import http_date
from veloce.http.response import FileResponse, JSONResponse, RedirectResponse, Response
from veloce.safe import safe_join
from veloce.signals import message_flashed
from veloce.status import HTTP_200_OK, HTTP_302_FOUND, HTTP_403_FORBIDDEN

# -- Context vars ------------------------------------------

# The active app is stashed on this ContextVar by `Veloce.handle_request`.
# `current_app` is a proxy that resolves to the active app on every
# attribute access - so `current_app.config["DEBUG"]` works from any
# coroutine/task spawned during request handling.
_current_app_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "veloce_current_app", default=None
)

# Outside-request stash for `app.test_request_context()` - the dispatch
# pipeline passes the live request through arguments, so this is only
# consulted by code that explicitly enters a test_request_context block.
_current_request_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "veloce_current_request", default=None
)


# -- Proxy classes -----------------------------------------


class _ContextProxy:
    """Shared base for ContextVar-backed proxies.

    Subclasses set `_var` (the ContextVar) and `_subject` (a short name
    used in the unbound error message). The default `_resolve` returns
    whatever the var carries; override it when the bound value is not
    the final target (see `_SessionProxy`, which resolves to
    `request.session`).
    """

    __slots__ = ()

    _var: contextvars.ContextVar[Any]
    _subject: str
    _scope_label: str = "request"

    def _resolve(self) -> Any:
        value = self._var.get()
        if value is None:
            raise RuntimeError(
                f"Working outside of {self._scope_label} context. "
                f"`{self._subject}` is only available while a request is being handled."
            )
        return value

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return super().__getattribute__(name)
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:
        value = self._var.get()
        if value is None:
            return f"<{self._subject}: unbound>"
        return f"<{self._subject}: {value!r}>"

    def __bool__(self) -> bool:
        return self._var.get() is not None


class _CurrentAppProxy(_ContextProxy):
    """Context-local proxy to the currently-handling Veloce app.

    Resolves to the app set by `Veloce.handle_request` for the duration
    of the request. Accessing any attribute outside that scope raises
    `RuntimeError`.
    """

    __slots__ = ()
    _var = _current_app_var
    _subject = "current_app"
    _scope_label = "application"


class _CurrentRequestProxy(_ContextProxy):
    """Context-local proxy to the request being handled.

    Resolves to the `Request` bound by the dispatcher (or by
    `app.test_request_context()`) for the duration of the request.
    Accessing any attribute outside that scope raises `RuntimeError`.
    """

    __slots__ = ()
    _var = _current_request_var
    _subject = "request"


class _SessionProxy(_ContextProxy):
    """Context-local proxy to the current request's session.

    Resolves to `request.session` (the dict `SessionMiddleware`
    maintains). Accessing it outside a request context, or without
    `SessionMiddleware` installed, raises `RuntimeError` - the same
    failure modes as `request` and `Request.session`.
    """

    __slots__ = ()
    _var = _current_request_var
    _subject = "session"

    def _resolve(self) -> Any:
        req = self._var.get()
        if req is None:
            raise RuntimeError(
                "Working outside of request context. `session` is only "
                "available while a request is being handled."
            )
        return req.session

    def __getitem__(self, key: str) -> Any:
        return self._resolve()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._resolve()[key] = value

    def __delitem__(self, key: str) -> None:
        del self._resolve()[key]

    def __contains__(self, key: str) -> bool:
        return key in self._resolve()

    def __iter__(self) -> Any:
        return iter(self._resolve())

    def __len__(self) -> int:
        return len(self._resolve())

    def __bool__(self) -> bool:
        req = self._var.get()
        return req is not None and "session" in req._state


class _RequestGlobals:
    """Request-scoped global object - the `g` namespace.

    Uses contextvars so it works in async without thread-local hacks.
    """

    _ctx_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
        "veloce_g", default=None
    )

    def _get_store(self) -> dict[str, Any]:
        store = self._ctx_var.get()
        if store is None:
            store = {}
            self._ctx_var.set(store)
        return store

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return super().__getattribute__(name)
        store = self._get_store()
        try:
            return store[name]
        except KeyError as err:
            raise AttributeError(f"'g' has no attribute '{name}'") from err

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self._get_store()[name] = value

    def __delattr__(self, name: str) -> None:
        store = self._get_store()
        try:
            del store[name]
        except KeyError as err:
            raise AttributeError(f"'g' has no attribute '{name}'") from err

    def __contains__(self, name: str) -> bool:
        return name in self._get_store()

    def get(self, name: str, default: Any = None) -> Any:
        return self._get_store().get(name, default)

    def pop(self, name: str, *args: Any) -> Any:
        return self._get_store().pop(name, *args)

    def setdefault(self, name: str, default: Any = None) -> Any:
        return self._get_store().setdefault(name, default)

    def _reset(self) -> None:
        """Reset g for a new request.

        Clears the var to `None` rather than binding a fresh dict -
        `_get_store` allocates lazily on first access, so a request whose
        handler never touches `g` pays no allocation.
        """
        self._ctx_var.set(None)


# -- Aborter -----------------------------------------------


def _default_detail(code: int) -> str:
    """Resolve the reason-phrase fallback used when no detail is supplied."""
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "Error"


class Aborter:
    """A callable that turns a status code into an HTTPException.

    Used as `app.aborter(404)` or `app.aborter(403, "Forbidden")`.
    Subclasses can override `mapping` to register custom exception
    classes for specific status codes; the base class leaves it empty
    so the default `exception_for_status` lookup applies.
    """

    mapping: dict[int, type] = {}

    def __init__(self, extra_mapping: dict[int, type] | None = None) -> None:
        self._mapping: dict[int, type] = {}
        if extra_mapping:
            self._mapping.update(extra_mapping)

    def __call__(
        self,
        code: int,
        detail: str = "",
        headers: dict[str, str] | None = None,
    ) -> NoReturn:
        if not detail:
            detail = _default_detail(code)
        cls = self._mapping.get(code) or self.mapping.get(code) or exception_for_status(code)
        raise cls(status_code=code, detail=detail, headers=headers)


# -- Singletons --------------------------------------------

# `from veloce import current_app`.
current_app = _CurrentAppProxy()

# `from veloce import request`.
request = _CurrentRequestProxy()

# `from veloce import session`.
session = _SessionProxy()

# `from veloce import g`.
g = _RequestGlobals()


# -- Context introspection ---------------------------------


def has_app_context() -> bool:
    """True iff `current_app` resolves to a real app.

    Use this to gate code that reads `current_app`/`app.config` so it
    can also run outside a request (e.g. helper modules imported at
    module-import time, before any app is bound to the contextvar).
    """
    return _current_app_var.get() is not None


def has_request_context() -> bool:
    """True iff a request is bound to this task/context.

    Veloce passes the live request through arguments during dispatch,
    so this only flips True inside `app.test_request_context()` blocks
    or when application code explicitly sets the contextvar.
    """
    return _current_request_var.get() is not None


# -- abort() -----------------------------------------------


def abort(status_code: int, detail: str = "", headers: dict[str, str] | None = None) -> NoReturn:
    """Raise an HTTPException - a concise shorthand.

    Raises the typed subclass for known status codes (e.g. `NotFound` for 404,
    `Forbidden` for 403) so error handlers registered against a specific
    subclass match. Unknown codes fall back to the bare `HTTPException`.

    Usage:
        abort(404)              # -> raises NotFound
        abort(403, "Forbidden") # -> raises Forbidden
    """
    if not detail:
        detail = _default_detail(status_code)
    cls = exception_for_status(status_code)
    raise cls(status_code=status_code, detail=detail, headers=headers)


# -- after_this_request() ----------------------------------


def after_this_request(func: Any) -> Any:
    """Register a one-shot after-request callback.

    Fires after the global `@app.after_request` hooks have run for the
    current request only - future requests are unaffected. Useful for
    work that depends on data computed inside the handler (e.g. setting
    a cookie whose value the handler decided).

    Returns the callback unchanged so it can be used as a decorator.
    Raises `RuntimeError` when called outside an active request.
    """
    request = _current_request_var.get()
    if request is None:
        raise RuntimeError("after_this_request() requires an active request context.")
    # List, not set: order matters - dispatcher drains in registration order.
    cbs = request._state.setdefault("_after_this_request", [])
    cbs.append(func)
    return func


# -- send_file() -------------------------------------------


def _attachment_name(resolved: str, as_attachment: bool, download_name: str | None) -> str | None:
    """Resolve the `Content-Disposition` filename for a file response.

    Returns `None` for inline responses; otherwise the explicit
    `download_name` or the basename of the resolved path.
    """
    if not as_attachment:
        return None
    return download_name or os.path.basename(resolved)


def _build_send_file_args(
    path_or_file: Any,
    as_attachment: bool,
    download_name: str | None,
    last_modified: Any,
    etag: bool | str,
    max_age: int | None,
) -> tuple[str, str | None, dict[str, str], bool]:
    """Compute the path, attachment name, headers, and strip-ETag flag.

    Shared by `send_file` and `async_send_file` so the conditional-GET header
    assembly and `etag=False` handling live in one place.
    """
    headers: dict[str, str] = {}
    if last_modified is not None:
        if isinstance(last_modified, str):
            headers[HEADER_LAST_MODIFIED] = last_modified
        else:
            headers[HEADER_LAST_MODIFIED] = http_date(last_modified)

    if isinstance(etag, str):
        headers[HEADER_ETAG] = etag

    if max_age is not None:
        headers[HEADER_CACHE_CONTROL] = f"public, max-age={max_age}"

    path = str(path_or_file)
    attachment_name = _attachment_name(path, as_attachment, download_name)
    return path, attachment_name, headers, etag is False


def _finish_send_file(resp: Response, strip_etag: bool) -> Response:
    """Apply the `etag=False` strip-and-reset to a built FileResponse."""
    if strip_etag:
        resp.headers.pop(HEADER_ETAG, None)
        resp._encoded = None
    return resp


def send_file(
    path_or_file: Any,
    mimetype: str | None = None,
    as_attachment: bool = False,
    download_name: str | None = None,
    last_modified: Any = None,
    etag: bool | str = True,
    max_age: int | None = None,
) -> Response:
    """Serve a file top-level helper.

    Accepts a filesystem path (str / PathLike) and returns a `FileResponse`
    with conditional-GET headers already set (Last-Modified, ETag - both
    were added by Q40/Q42). Optional knobs:

    - `mimetype=` overrides the auto-guessed content type.
    - `as_attachment=True` sets `Content-Disposition: attachment;
      filename=<download_name or basename>`.
    - `download_name=` overrides the filename in `Content-Disposition`.
    - `last_modified=` overrides the file's mtime (datetime, unix ts,
      or pre-formatted IMF-fixdate string).
    - `etag=False` suppresses the auto-generated ETag; `etag="<value>"`
      uses the caller-provided one verbatim (already-quoted).
    - `max_age=` adds `Cache-Control: public, max-age=<n>`.
    """
    path, attachment_name, headers, strip_etag = _build_send_file_args(
        path_or_file, as_attachment, download_name, last_modified, etag, max_age
    )
    resp = FileResponse(
        path=path,
        filename=attachment_name,
        content_type=mimetype,
        headers=headers,
    )
    return _finish_send_file(resp, strip_etag)


async def async_send_file(
    path_or_file: Any,
    mimetype: str | None = None,
    as_attachment: bool = False,
    download_name: str | None = None,
    last_modified: Any = None,
    etag: bool | str = True,
    max_age: int | None = None,
) -> Response:
    """Serve a file - async variant of `send_file`.

    Identical to `send_file` but reads the file in an executor via
    `FileResponse.from_path`, so it never blocks the event loop. Prefer
    this from async handlers; the sync `send_file` emits a
    `DeprecationWarning` when called on a running loop.
    """
    path, attachment_name, headers, strip_etag = _build_send_file_args(
        path_or_file, as_attachment, download_name, last_modified, etag, max_age
    )
    resp = await FileResponse.from_path(
        path=path,
        filename=attachment_name,
        content_type=mimetype,
        headers=headers,
    )
    return _finish_send_file(resp, strip_etag)


# -- redirect() --------------------------------------------


def redirect(
    location: str,
    code: int = HTTP_302_FOUND,
    headers: dict[str, str] | None = None,
) -> Response:
    """Build a redirect response helper.

    Default `code=302` matches the long-standing convention. RFC 9110 Sec. 15.4
    catalogue: 301 (permanent, method may change), 302 (found, method
    may change), 303 (see other, method becomes GET), 307 (temporary,
    method preserved), 308 (permanent, method preserved). Pick the one
    that matches your semantics - the helper is a thin wrapper, not a
    policy. Accepts extra headers (e.g. `Vary`).
    """
    return RedirectResponse(location, status_code=code, headers=headers)


# -- jsonify() ---------------------------------------------


def jsonify(*args: Any, **kwargs: Any) -> JSONResponse:
    """Create a JSON response - a concise shorthand.

    Honours two app-config flags when called inside a request:
    - `JSON_SORT_KEYS` (default True) - sort dict keys alphabetically.
    - `JSONIFY_PRETTYPRINT_REGULAR` (default False) - indent the output
      with 2 spaces for readability. Often enabled under DEBUG.

    Usage:
        return jsonify(name="alice", age=30)
        return jsonify({"name": "alice"})
        return jsonify([1, 2, 3])
    """
    if args and kwargs:
        raise TypeError("jsonify() takes either positional or keyword args, not both")
    data = (args[0] if len(args) == 1 else list(args)) if args else kwargs

    # Inside a request, serialise through the app's JSON provider so jsonify
    # and `app.json.dumps()` share one source of truth (the provider caches
    # the config bitmask on first access). Outside a request context there is
    # no app, so fall back to the plain JSONResponse defaults.
    app = _current_app_var.get()
    if app is not None:
        return app.json.response(data)
    return JSONResponse(data)


# -- make_response() ---------------------------------------


def make_response(
    body: Any = b"",
    status_code: int = HTTP_200_OK,
    headers: dict[str, str] | None = None,
    content_type: str | None = None,
) -> Response:
    """Create a Response - a convenience wrapper.

    Usage:
        resp = make_response("Hello", 200)
        resp = make_response({"data": True}, 201)
    """
    if isinstance(body, (dict, list)):
        return JSONResponse(body, status_code=status_code, headers=headers)
    if isinstance(body, str):
        ct = content_type or MIME_HTML
        return Response(
            status_code=status_code,
            body=body.encode("utf-8"),
            content_type=ct,
            headers=headers,
        )
    if isinstance(body, bytes):
        ct = content_type or MIME_OCTET
        return Response(
            status_code=status_code,
            body=body,
            content_type=ct,
            headers=headers,
        )
    # Pydantic model
    if hasattr(body, "model_dump"):
        return JSONResponse(body.model_dump(), status_code=status_code, headers=headers)
    return JSONResponse(body, status_code=status_code, headers=headers)


# -- send_from_directory() ---------------------------------


def send_from_directory(
    directory: str,
    filename: str,
    mimetype: str | None = None,
    as_attachment: bool = False,
    download_name: str | None = None,
) -> FileResponse:
    """Send a file from a directory (sync version).

    Traversal-safe via `safe_join`. Returns 403 on any escape attempt.

    For async, use send_from_directory_async() instead.
    """

    resolved = safe_join(directory, filename)
    if resolved is None:
        abort(HTTP_403_FORBIDDEN, MSG_ACCESS_DENIED)

    attachment_name = _attachment_name(str(resolved), as_attachment, download_name)
    return FileResponse(
        path=resolved,
        filename=attachment_name,
        content_type=mimetype,
    )


async def send_from_directory_async(
    directory: str,
    filename: str,
    mimetype: str | None = None,
    as_attachment: bool = False,
    download_name: str | None = None,
) -> FileResponse:
    """Send a file from a directory - async version, reads file in executor.

    Traversal-safe via `safe_join`.
    """

    # `safe_join` is pure string arithmetic; the file read happens below.
    resolved = safe_join(directory, filename)  # noqa: ASYNC240
    if resolved is None:
        abort(HTTP_403_FORBIDDEN, MSG_ACCESS_DENIED)

    attachment_name = _attachment_name(str(resolved), as_attachment, download_name)
    return await FileResponse.from_path(
        path=resolved,
        filename=attachment_name,
        content_type=mimetype,
    )


# -- flash() / get_flashed_messages() ----------------------


def _flash_store() -> Any:
    """Resolve the session dict the flash queue lives in.

    A previous implementation wrote to `g` (a per-request `contextvars`
    namespace), which made `flash()` silently lose the message across
    the POST/redirect/GET round-trip that flashes are designed for -
    the redirected GET sees a fresh `g` with no `_flashes` key. Now
    we route through the active session so the signed-cookie /
    server-side session carries the queue across requests, matching
    the round-trip contract the docstring promises.
    """
    req = _current_request_var.get()
    if req is None or "session" not in req._state:
        raise RuntimeError(
            "flash() requires an active request with SessionMiddleware "
            "(or ServerSessionMiddleware) installed - the flashes are "
            "carried across requests via the session, not the per-request `g`."
        )
    return req._state["session"]


def flash(message: str, category: str = "message") -> None:
    """Flash a message for the next request - requires SessionMiddleware.

    Usage:
        flash("Item created successfully")
        flash("Invalid input", "error")
    """
    store = _flash_store()
    flashes = store.setdefault("_flashes", [])
    flashes.append((category, message))
    # `Session.setdefault` of an unseen key flips `.modified`, but the
    # subsequent `flashes.append(...)` mutates an existing list - that
    # mutation is invisible to the Session container, so the cookie
    # would not be re-signed if `setdefault` short-circuited (key
    # already present). Mark explicitly to cover both paths.
    with contextlib.suppress(AttributeError):
        store.modified = True
    # `message_flashed` signal - fires for every flash() call.
    message_flashed.send(_current_app_var.get(), message=message, category=category)


def get_flashed_messages(
    with_categories: bool = False, category_filter: Sequence[str] | None = None
) -> list:
    """Get flashed messages - call in templates.

    Usage:
        messages = get_flashed_messages()
        messages = get_flashed_messages(with_categories=True)
    """
    req = _current_request_var.get()
    # Reading without an active request returns an empty list - calling
    # this from a template render outside a request shouldn't crash.
    if req is None or "session" not in req._state:
        return []
    store = req._state["session"]
    # `Session.pop` already flips `.modified` when the key existed -
    # no need to set it again here. Non-Session dict subclasses that
    # don't override `pop` won't carry the flag, but their callers
    # aren't using the cookie middleware anyway.
    flashes = store.pop("_flashes", [])
    if category_filter:
        allowed = set(category_filter)
        flashes = [(cat, msg) for cat, msg in flashes if cat in allowed]
    if with_categories:
        return flashes
    return [msg for _, msg in flashes]


# -- stream_with_context() ---------------------------------


def stream_with_context(generator: Any) -> Any:
    """Keep the request context alive while a streaming generator runs.

    A streaming response body is consumed by the ASGI
    emit layer *after* the handler has returned, by which point the
    request context has been torn down - so a generator that touches
    `request`, `g`, or `current_app` would fail. Wrap it::

        return StreamingResponse(stream_with_context(generate()))

    The current request / app / `g` snapshot is captured now and
    re-established for the lifetime of the wrapped iteration. Accepts
    either an async or a synchronous generator/iterable.
    """
    captured_app = _current_app_var.get()
    captured_req = _current_request_var.get()
    captured_g = _RequestGlobals._ctx_var.get()

    async def _ctx_keeping() -> Any:
        tok_app = _current_app_var.set(captured_app)
        tok_req = _current_request_var.set(captured_req)
        tok_g = _RequestGlobals._ctx_var.set(captured_g)
        try:
            if hasattr(generator, "__aiter__"):
                async for chunk in generator:
                    yield chunk
            else:
                for chunk in generator:
                    yield chunk
        finally:
            _RequestGlobals._ctx_var.reset(tok_g)
            _current_request_var.reset(tok_req)
            _current_app_var.reset(tok_app)

    return _ctx_keeping()
